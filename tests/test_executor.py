import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from binance_spot_fake import FakeSpot
from tbot.core.config import StrategyConfig
from tbot.core.models import Fill
from tbot.core.timeframe import Timeframe
from tbot.exchange.binance import (
    NOTHING_TO_CANCEL,
    TESTNET_URL,
    BinanceError,
    BinanceSpot,
    Order,
    SymbolRules,
)
from tbot.live.config import LiveConfig
from tbot.live.executor import TAG_META, BinanceBookTicker, LiveExecutor, order_id
from tbot.live.ledger import Adjustment, Ledger
from tbot.live.runner import (
    BUDGET_META,
    BUDGET_NOTE,
    GUARD_META,
    OWED_META,
    budget_owed,
    check_budget,
    reconcile_round,
)
from tbot.portfolio.portfolio import Portfolio
from tbot.risk.guard import GuardConfig, GuardState, Mode, RiskGuard

T0 = datetime(2024, 1, 1, tzinfo=UTC)
BTC = "BTCUSDT"
MARKS = {BTC: 50000.0}
ASK = 50000.0 * 1.0001
BID = 50000.0 * 0.9999


class Collect:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


def run[T](
    fake: FakeSpot,
    tmp_path: Path,
    action: Callable[[LiveExecutor, Ledger, Collect], Awaitable[T]],
    stop_pct: float = 0.2,
) -> T:
    async def go() -> T:
        async with fake.client() as client:
            spot = BinanceSpot(
                client, fake.api_key, fake.secret, base_url=TESTNET_URL, clock=lambda: T0
            )
            await spot.load_rules([BTC, "ETHUSDT"])
            ledger, notifier = Ledger(tmp_path / "ledger.sqlite"), Collect()
            executor = LiveExecutor(
                spot, ledger, notifier, lambda: T0, fee_rate=0.001, protective_stop_pct=stop_pct
            )
            try:
                return await action(executor, ledger, notifier)
            finally:
                ledger.close()

    return asyncio.run(go())


@pytest.fixture(autouse=True)
def fast_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tbot.live.executor.LOOKUP_DELAYS", (0.0, 0.0, 0.0))


def statuses(ledger: Ledger) -> list[str]:
    rows = ledger.conn.execute("SELECT status FROM orders ORDER BY id").fetchall()
    return [row[0] for row in rows]


def test_order_id_is_deterministic() -> None:
    assert order_id(T0, BTC, 1.0) == "tb1704067200000BTCUSDTB"
    assert order_id(T0, BTC, -1.0) == "tb1704067200000BTCUSDTS"
    assert order_id(T0, BTC, 1.0, attempt=2) == "tb1704067200000BTCUSDTBr2"
    assert len(order_id(T0, "A" * 40, 1.0)) == 36
    assert len(order_id(T0, "A" * 40, 1.0, attempt=12)) == 36
    assert order_id(T0, BTC, 1.0, tag="a1b2") == "tba1b21704067200000BTCUSDTB"


def test_buy_shrinks_by_base_commission_and_places_stop(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> Portfolio:
        portfolio = Portfolio(1000.0)
        fills = await executor.execute({BTC: 0.01}, T0, portfolio, MARKS)
        await executor.after_event(portfolio, MARKS)
        assert len(fills) == 1
        assert fills[0].quantity == pytest.approx(0.00999)  # 0.01 minus BTC commission
        assert fills[0].price == pytest.approx(ASK)
        assert fills[0].fee == pytest.approx(0.00001 * ASK)
        assert ledger.fills() == fills
        assert statuses(ledger) == ["pending", "filled"]
        assert notifier.messages[0].startswith("[live] BUY 0.009990 BTCUSDT")
        return portfolio

    portfolio = run(fake, tmp_path, action)
    assert portfolio.position(BTC) == pytest.approx(0.00999)
    assert portfolio.cash == pytest.approx(fake.balances["USDT"])  # book equals exchange
    assert fake.balances["BTC"] + fake.locked["BTC"] == pytest.approx(0.00999)
    [stop] = [o for o in fake.orders if o["type"] == "STOP_LOSS_LIMIT"]
    assert (stop["status"], stop["origQty"]) == ("NEW", "0.00999000")
    assert (stop["stopPrice"], stop["price"]) == ("40000.00", "39800.00")


def test_sell_cancels_stop_and_pays_quote_fee(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 100.0, "BTC": 0.5}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> Portfolio:
        portfolio = Portfolio(100.0)
        portfolio.positions[BTC] = 0.5
        await executor.after_event(portfolio, MARKS)  # stop locks the whole position
        assert fake.locked["BTC"] == pytest.approx(0.5)
        [fill] = await executor.execute({BTC: -0.2}, T0, portfolio, MARKS)
        await executor.after_event(portfolio, MARKS)
        assert fill.quantity == pytest.approx(-0.2)
        assert fill.fee == pytest.approx(0.2 * BID * 0.001)
        return portfolio

    portfolio = run(fake, tmp_path, action)
    kinds = [(o["type"], o["status"]) for o in fake.orders]
    assert kinds == [
        ("STOP_LOSS_LIMIT", "CANCELED"),
        ("MARKET", "FILLED"),
        ("STOP_LOSS_LIMIT", "NEW"),
    ]
    assert fake.orders[-1]["origQty"] == "0.30000000"
    assert portfolio.cash == pytest.approx(fake.balances["USDT"])


def test_lost_response_is_recovered_by_client_order_id(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})
    fake.lose_next_order_response = True

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> Portfolio:
        portfolio = Portfolio(1000.0)
        fills = await executor.execute({BTC: 0.01}, T0, portfolio, MARKS)
        assert len(fills) == 1
        assert statuses(ledger) == ["pending", "filled"]
        # A second decision at the same close (a stream arrived late) is a new order.
        again = await executor.execute({BTC: 0.005}, T0, portfolio, MARKS)
        assert len(again) == 1
        assert len(ledger.fills()) == 2
        return portfolio

    portfolio = run(fake, tmp_path, action, stop_pct=0.0)
    tag = fake.orders[0]["clientOrderId"][2:6]  # the session's own
    ids = [o["clientOrderId"] for o in fake.orders]
    assert ids == [f"tb{tag}1704067200000BTCUSDTB", f"tb{tag}1704067200000BTCUSDTBr2"]
    assert portfolio.position(BTC) == pytest.approx(fake.balances["BTC"])  # none twice


def test_below_minimum_is_skipped_and_cash_caps_buys(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 100.0}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> None:
        portfolio = Portfolio(100.0)
        assert await executor.execute({BTC: 0.00005}, T0, portfolio, MARKS) == []
        assert statuses(ledger) == ["skipped"]
        [fill] = await executor.execute({BTC: 1.0}, T0, portfolio, MARKS)
        assert fill.quantity <= 100 / ASK
        assert fill.quantity == pytest.approx(0.00199 * 0.999)

    run(fake, tmp_path, action, stop_pct=0.0)
    assert fake.order_count("MARKET") == 1
    assert fake.order_count("STOP_LOSS_LIMIT") == 0


def test_bnb_commission_is_priced_in_quote(tmp_path: Path) -> None:
    fake = FakeSpot(
        balances={"USDT": 1000.0, "BNB": 1.0},
        prices={BTC: 50000.0, "BNBUSDT": 300.0},
        fee_asset="BNB",
    )

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> None:
        [fill] = await executor.execute({BTC: 0.01}, T0, Portfolio(1000.0), MARKS)
        assert fill.quantity == pytest.approx(0.01)
        assert fill.fee == pytest.approx(0.01 * ASK * 0.001, rel=1e-4)  # BNB amount is rounded

    run(fake, tmp_path, action, stop_pct=0.0)


def test_exchange_error_is_reported(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> None:
        fake.fail_next = [httpx.Response(400, json={"code": -1021, "msg": "Timestamp outside"})]
        assert await executor.execute({BTC: 0.01}, T0, Portfolio(1000.0), MARKS) == []
        assert statuses(ledger) == ["failed"]
        assert "order failed for BTCUSDT" in notifier.messages[0]

    run(fake, tmp_path, action)


def test_stop_gone_before_cancel_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> int:
        portfolio = Portfolio(1000.0)
        await executor.execute({BTC: 0.01}, T0, portfolio, MARKS)
        await executor.after_event(portfolio, MARKS)
        assert fake.order_count("STOP_LOSS_LIMIT") == 1

        async def gone(symbol: str, order_id: int) -> Order:  # triggered a moment ago
            raise BinanceError(NOTHING_TO_CANCEL, "Unknown order sent.", 400)

        monkeypatch.setattr(executor.spot, "cancel_order", gone)
        return await executor.cancel_stops(BTC, portfolio)

    assert run(fake, tmp_path, action) == 0


def test_order_in_doubt_after_a_server_error_is_looked_up(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})
    fake.error_after_next_order = httpx.Response(503)  # executed, but the answer is an error

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> Portfolio:
        portfolio = Portfolio(1000.0)
        [fill] = await executor.execute({BTC: 0.01}, T0, portfolio, MARKS)
        assert fill.quantity == pytest.approx(0.00999)  # commission read from its trades
        assert statuses(ledger) == ["pending", "filled"]
        return portfolio

    portfolio = run(fake, tmp_path, action, stop_pct=0.0)
    assert fake.order_count("MARKET") == 1
    assert portfolio.cash == pytest.approx(fake.balances["USDT"])


def test_order_state_unknown_is_settled_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})
    fake.error_after_next_order = httpx.Response(503)

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> Portfolio:
        portfolio = Portfolio(1000.0)
        lookup = executor.spot.get_order

        async def unreachable(symbol: str, client_id: str) -> Order | None:
            raise httpx.ConnectError("down")

        monkeypatch.setattr(executor.spot, "get_order", unreachable)
        assert await executor.execute({BTC: 0.01}, T0, portfolio, MARKS) == []
        assert statuses(ledger) == ["pending", "unknown"]
        assert "state unknown" in notifier.messages[0]
        monkeypatch.setattr(executor.spot, "get_order", lookup)
        await executor.settle(portfolio)
        assert statuses(ledger) == ["pending", "unknown", "filled"]
        assert ledger.unresolved_orders() == []
        return portfolio

    portfolio = run(fake, tmp_path, action, stop_pct=0.0)
    assert portfolio.position(BTC) == pytest.approx(fake.balances["BTC"])


def test_settle_resolves_orders_left_by_a_crash(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> Portfolio:
        # A previous run wrote two pending rows and died; only one order reached the exchange.
        sent, lost = order_id(T0, BTC, 1.0), order_id(T0, "ETHUSDT", 1.0)
        ledger.add_order(T0, BTC, 0.01, "pending", "", sent)
        ledger.add_order(T0, "ETHUSDT", 0.1, "pending", "", lost)
        await executor.spot.market_order(BTC, 0.01, sent)
        portfolio = Portfolio(1000.0)
        await executor.settle(portfolio)
        assert ledger.unresolved_orders() == []
        rows = ledger.conn.execute(
            "SELECT client_id, status, note FROM orders WHERE id > 2 ORDER BY id"
        ).fetchall()
        assert rows == [(sent, "filled", ""), (lost, "failed", "not on exchange")]
        assert "booked an order" in notifier.messages[0]
        return portfolio

    portfolio = run(fake, tmp_path, action)
    assert portfolio.position(BTC) == pytest.approx(0.00999)


def test_triggered_stop_is_booked_as_a_fill(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> Portfolio:
        portfolio = Portfolio(1000.0)
        await executor.execute({BTC: 0.01}, T0, portfolio, MARKS)
        await executor.after_event(portfolio, MARKS)
        fake.trigger_stop(BTC)  # the market fell through the stop between events
        await executor.settle(portfolio)
        assert portfolio.position(BTC) == pytest.approx(0.0, abs=1e-12)
        assert ledger.get_meta("stop:" + BTC) is None
        assert any("protective stop sold" in m for m in notifier.messages)
        await executor.settle(portfolio)  # booked once
        assert len(ledger.fills()) == 2
        return portfolio

    portfolio = run(fake, tmp_path, action)
    assert portfolio.cash == pytest.approx(fake.balances["USDT"], abs=0.01)
    assert len(portfolio.trades) == 1


def test_buys_never_spend_more_than_the_book_holds(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> None:
        portfolio = Portfolio(100.0)  # the bot's budget; the other 900 USDT are the owner's
        [fill] = await executor.execute({BTC: 0.01}, T0, portfolio, MARKS)
        assert fill.quantity * fill.price < 100.0
        assert portfolio.cash >= 0.0

    run(fake, tmp_path, action, stop_pct=0.0)
    assert fake.balances["USDT"] > 900.0


def test_a_sell_never_takes_the_owners_coins(tmp_path: Path) -> None:
    # Budget mode: the owner holds 0.3 BTC, and the bot's stop fills before its exit order.
    fake = FakeSpot(balances={"USDT": 5000.0, "BTC": 0.3}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> Portfolio:
        portfolio = Portfolio(1000.0)
        await executor.execute({BTC: 0.01}, T0, portfolio, MARKS)
        await executor.after_event(portfolio, MARKS)
        planned = {BTC: -portfolio.position(BTC)}  # from the book before the stop is booked
        fake.trigger_stop(BTC)
        assert await executor.execute(planned, T0, portfolio, MARKS) == []
        return portfolio

    portfolio = run(fake, tmp_path, action)
    assert portfolio.position(BTC) == pytest.approx(0.0, abs=1e-12)
    assert fake.balances["BTC"] + fake.locked.get("BTC", 0.0) == pytest.approx(0.3)
    assert fake.order_count("MARKET") == 1


def test_negative_book_cash_never_turns_a_buy_into_a_sell(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0, "BTC": 0.1}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> None:
        portfolio = Portfolio(1000.0)
        portfolio.adjust("", 0.0, -1020.0)  # overspent: book cash -20
        assert await executor.execute({BTC: 0.01}, T0, portfolio, MARKS) == []

    run(fake, tmp_path, action)
    assert fake.orders == []


def test_a_stop_is_booked_once_even_if_the_bot_dies_while_alerting(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})

    class Killed(Collect):
        async def send(self, text: str) -> bool:
            raise asyncio.CancelledError  # a hard stop while Telegram is slow

    async def go() -> None:
        async with fake.client() as client:
            spot = BinanceSpot(
                client, fake.api_key, fake.secret, base_url=TESTNET_URL, clock=lambda: T0
            )
            await spot.load_rules([BTC, "ETHUSDT"])
            path = tmp_path / "ledger.sqlite"
            with Ledger(path) as ledger:
                executor = LiveExecutor(
                    spot, ledger, Collect(), lambda: T0, fee_rate=0.001, protective_stop_pct=0.2
                )
                portfolio = Portfolio(1000.0)
                await executor.execute({BTC: 0.01}, T0, portfolio, MARKS)
                await executor.after_event(portfolio, MARKS)
                fake.trigger_stop(BTC)
                executor.notifier = Killed()
                with pytest.raises(asyncio.CancelledError):
                    await executor.settle(portfolio)
            with Ledger(path) as ledger:  # the restart
                executor = LiveExecutor(
                    spot, ledger, Collect(), lambda: T0, fee_rate=0.001, protective_stop_pct=0.2
                )
                portfolio = Portfolio(1000.0)
                for fill in ledger.fills():
                    portfolio.apply(fill)
                assert await executor.settle(portfolio) == 0
                assert len(ledger.fills()) == 2
                assert portfolio.position(BTC) == pytest.approx(0.0, abs=1e-12)

    asyncio.run(go())


def test_a_missing_stop_is_placed_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> Collect:
        portfolio = Portfolio(1000.0)
        await executor.execute({BTC: 0.01}, T0, portfolio, MARKS)
        place = executor.spot.stop_loss_order

        async def down(*args: object) -> Order:
            raise httpx.ConnectError("down")

        monkeypatch.setattr(executor.spot, "stop_loss_order", down)
        await executor.after_event(portfolio, MARKS)
        await executor.after_event(portfolio, MARKS)
        assert executor.unprotected == {BTC}
        monkeypatch.setattr(executor.spot, "stop_loss_order", place)
        await executor.protect(portfolio, MARKS)  # what reconciliation does
        assert executor.unprotected == set()

        [stop] = [o for o in fake.orders if o["type"] == "STOP_LOSS_LIMIT"]
        await executor.spot.cancel_order(BTC, stop["orderId"])  # the owner cancels it
        assert await executor.settle(portfolio) == 0
        assert executor.unprotected == {BTC}
        await executor.protect(portfolio, MARKS)
        return notifier

    notifier = run(fake, tmp_path, action)
    assert sum(o["status"] == "NEW" for o in fake.orders if o["type"] == "STOP_LOSS_LIMIT") == 1
    assert sum("protective stop failed" in m for m in notifier.messages) == 1  # not every retry


def test_an_unexpected_error_leaves_the_order_to_be_looked_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> Portfolio:
        portfolio = Portfolio(1000.0)
        trades = executor.spot.my_trades

        async def garbled(symbol: str, order_id: int) -> list[object]:
            raise ValueError("not JSON")

        fake.error_after_next_order = httpx.Response(503)  # so the fill comes from my_trades
        monkeypatch.setattr(executor.spot, "my_trades", garbled)
        assert await executor.execute({BTC: 0.01}, T0, portfolio, MARKS) == []
        assert statuses(ledger) == ["pending", "unknown"]
        monkeypatch.setattr(executor.spot, "my_trades", trades)
        assert await executor.settle(portfolio) == 1
        return portfolio

    portfolio = run(fake, tmp_path, action, stop_pct=0.0)
    assert portfolio.position(BTC) == pytest.approx(fake.balances["BTC"])


def test_an_order_lost_on_the_way_is_failed_only_after_a_second_look(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> None:
        async def lost(symbol: str, quantity: float, client_id: str) -> Order:
            raise BinanceError(0, "Service Unavailable", 503)  # never reached the engine

        monkeypatch.setattr(executor.spot, "market_order", lost)
        assert await executor.execute({BTC: 0.01}, T0, Portfolio(1000.0), MARKS) == []
        assert statuses(ledger) == ["pending", "unknown"]  # it could still show up
        assert await executor.settle(Portfolio(1000.0)) == 0
        assert statuses(ledger) == ["pending", "unknown", "failed"]

    run(fake, tmp_path, action, stop_pct=0.0)
    assert fake.orders == []


def test_a_failed_stop_replacement_is_alerted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> Collect:
        portfolio = Portfolio(1000.0)
        await executor.execute({BTC: 0.01}, T0, portfolio, MARKS)
        await executor.after_event(portfolio, MARKS)  # the first stop is placed
        place = executor.spot.stop_loss_order

        async def rejected(*args: object) -> Order:
            raise BinanceError(-2010, "Stop price would trigger immediately.", 400)

        monkeypatch.setattr(executor.spot, "stop_loss_order", rejected)
        await executor.after_event(portfolio, MARKS)  # the old stop is cancelled first
        await executor.protect(portfolio, MARKS)
        monkeypatch.setattr(executor.spot, "stop_loss_order", place)
        await executor.protect(portfolio, MARKS)
        return notifier

    messages = run(fake, tmp_path, action).messages
    failed = [m for m in messages if "protective stop failed" in m]
    assert len(failed) == 1
    assert "has no exchange stop" in failed[0]
    assert messages[-1].endswith("protective stop for BTCUSDT placed")


def test_an_order_booked_between_bars_gets_its_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})
    fake.error_after_next_order = httpx.Response(503)  # filled, but the answer is lost
    config = LiveConfig(
        mode="testnet",
        initial_cash=1000.0,
        strategies=[
            StrategyConfig(
                name="donchian_trend", symbols=[BTC], timeframe=Timeframe.H4, allocation=1.0
            )
        ],
        ledger=tmp_path / "unused.sqlite",
    )

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> Portfolio:
        portfolio = Portfolio(1000.0)
        lookup = executor.spot.get_order

        async def unreachable(symbol: str, client_id: str) -> Order | None:
            raise httpx.ConnectError("down")

        monkeypatch.setattr(executor.spot, "get_order", unreachable)
        await executor.execute({BTC: 0.01}, T0, portfolio, MARKS)
        await executor.after_event(portfolio, MARKS)  # the book holds nothing yet: no stop
        monkeypatch.setattr(executor.spot, "get_order", lookup)
        guard = RiskGuard(GuardConfig())
        await reconcile_round(
            executor, portfolio, MARKS, ledger, notifier, guard, config, lambda: T0
        )
        return portfolio

    portfolio = run(fake, tmp_path, action)
    assert portfolio.position(BTC) == pytest.approx(0.00999)
    stops = [o for o in fake.orders if o["type"] == "STOP_LOSS_LIMIT" and o["status"] == "NEW"]
    assert [float(o["origQty"]) for o in stops] == [pytest.approx(0.00999)]


def _live_config(tmp_path: Path) -> LiveConfig:
    return LiveConfig(
        mode="testnet",
        initial_cash=1000.0,
        strategies=[
            StrategyConfig(
                name="donchian_trend", symbols=[BTC], timeframe=Timeframe.H4, allocation=1.0
            )
        ],
        ledger=tmp_path / "unused.sqlite",
    )


def test_an_exit_sells_what_the_exchange_holds_when_the_book_says_more(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 500.0, "BTC": 0.008}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> list[Fill]:
        portfolio = Portfolio(500.0)
        portfolio.positions[BTC] = 0.01  # a commission in BTC the book missed, say
        return await executor.execute({BTC: -0.01}, T0, portfolio, MARKS)

    [fill] = run(fake, tmp_path, action, stop_pct=0.0)
    assert fill.quantity == pytest.approx(-0.008)  # not refused for the full 0.01


def test_a_buy_spends_no_more_than_the_account_holds(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 300.0}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> None:
        await executor.execute({BTC: 0.01}, T0, Portfolio(1000.0), MARKS)

    run(fake, tmp_path, action, stop_pct=0.0)
    [order] = [o for o in fake.orders if o["type"] == "MARKET"]
    # Free USDT over the ask with the fee and the safety margin, rounded down to the step.
    assert float(order["origQty"]) == pytest.approx(0.00598)


def test_money_taken_out_moves_the_guard_and_keeps_a_resume(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 600.0}, prices={BTC: 50000.0})  # 400 withdrawn
    config = _live_config(tmp_path)

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> RiskGuard:
        portfolio = Portfolio(1000.0)
        guard = RiskGuard(GuardConfig(max_drawdown=0.3))
        guard.check(T0, 1000.0, T0)  # peak 1000
        guard.halt("an earlier halt")
        resumed = GuardState.model_validate_json(guard.state.model_dump_json())
        resumed.halted, resumed.halt_reason, resumed.peak_equity = False, "", 1000.0
        ledger.set_meta(GUARD_META, resumed.model_dump_json())  # `tbot resume` meanwhile
        await reconcile_round(
            executor, portfolio, MARKS, ledger, notifier, guard, config, lambda: T0
        )
        assert portfolio.cash == pytest.approx(600.0)
        return guard

    guard = run(fake, tmp_path, action, stop_pct=0.0)
    assert not guard.state.halted  # the resume survived the shift
    assert guard.state.peak_equity == pytest.approx(600.0)  # a transfer, not a 40% loss
    assert guard.check(T0, 600.0, T0).mode == Mode.NORMAL


def test_an_order_in_doubt_blocks_the_next_order_for_its_symbol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 1.01}, prices={BTC: 50000.0})  # owner: 1 BTC

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> None:
        portfolio = Portfolio(0.0)
        portfolio.apply(Fill(T0, BTC, 0.01, 50000.0, 0.0))  # the bot's 0.01 BTC
        fake.error_after_next_order = httpx.Response(503)  # it executes; the answer is lost
        lookup = executor.spot.get_order

        async def unreachable(symbol: str, client_id: str) -> Order | None:
            raise httpx.ConnectError("down")

        monkeypatch.setattr(executor.spot, "get_order", unreachable)
        await executor.execute({BTC: -0.01}, T0, portfolio, MARKS)  # the exit, in doubt
        monkeypatch.setattr(executor.spot, "get_order", lookup)
        # The next bar comes before any reconciliation and plans the same exit again.
        assert await executor.execute({BTC: -0.01}, T0, portfolio, MARKS) == []
        assert portfolio.position(BTC) == pytest.approx(0.0)  # booked from the lookup
        assert "no order this bar" in notifier.messages[-1]

    run(fake, tmp_path, action, stop_pct=0.0)
    assert fake.order_count("MARKET") == 1
    assert fake.balances["BTC"] == pytest.approx(1.0)  # the owner's coin is untouched


def test_no_stop_is_sized_while_a_sell_is_in_doubt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 1.01}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> set[str]:
        portfolio = Portfolio(0.0)
        portfolio.apply(Fill(T0, BTC, 0.01, 50000.0, 0.0))
        fake.error_after_next_order = httpx.Response(503)

        async def unreachable(symbol: str, client_id: str) -> Order | None:
            raise httpx.ConnectError("down")

        monkeypatch.setattr(executor.spot, "get_order", unreachable)
        await executor.execute({BTC: -0.01}, T0, portfolio, MARKS)
        await executor.after_event(portfolio, MARKS)  # the book still says 0.01
        return executor.unprotected

    assert run(fake, tmp_path, action) == {BTC}  # placed after reconciliation settles it
    assert fake.order_count("STOP_LOSS_LIMIT") == 0  # not on the owner's coins


def test_a_partly_filled_stop_keeps_working(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.1}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> Portfolio:
        portfolio = Portfolio(0.0)
        portfolio.apply(Fill(T0, BTC, 0.1, 50000.0, 0.0))
        await executor.after_event(portfolio, MARKS)  # stop 40000 for 0.1 BTC
        stop = next(o for o in fake.orders if o["type"] == "STOP_LOSS_LIMIT")
        # A gap through the stop: 0.03 filled at the limit, 0.07 still rests there.
        stop.update(executedQty="0.03000000", cummulativeQuoteQty=f"{0.03 * 39800:.8f}")
        fake.locked["BTC"] -= 0.03
        fake.balances["USDT"] = 0.03 * 39800
        fake.prices[BTC] = 39500.0
        assert await executor.settle(portfolio) == 1
        await executor.after_event(portfolio, MARKS)  # what reconciliation does after a fill
        assert stop["status"] == "NEW"  # not cancelled for a stop above the market
        return portfolio

    portfolio = run(fake, tmp_path, action)
    assert portfolio.position(BTC) == pytest.approx(0.07)
    assert fake.order_count("STOP_LOSS_LIMIT") == 1


def test_a_fill_while_balances_are_read_waits_for_the_next_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})
    compared: list[int] = []

    async def no_reconcile(*args: object, **kwargs: object) -> list[object]:
        compared.append(1)
        return []

    monkeypatch.setattr("tbot.live.runner.reconcile", no_reconcile)

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> None:
        settled = iter([0, 1])  # a stop executes while the balances are being read

        async def settle(portfolio: Portfolio) -> int:
            return next(settled)

        monkeypatch.setattr(executor, "settle", settle)
        guard = RiskGuard(GuardConfig())
        config = _live_config(tmp_path)
        await reconcile_round(
            executor, Portfolio(1000.0), MARKS, ledger, notifier, guard, config, lambda: T0
        )

    run(fake, tmp_path, action)
    assert compared == []  # balances that may or may not show the fill are not compared


def test_an_order_whose_trades_are_not_listed_yet_is_booked_at_the_usual_commission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> Portfolio:
        portfolio = Portfolio(1000.0)

        async def none_yet(symbol: str, order_id: int) -> list[object]:
            return []

        fake.error_after_next_order = httpx.Response(503)  # found again by lookup
        monkeypatch.setattr(executor.spot, "my_trades", none_yet)
        [fill] = await executor.execute({BTC: 0.01}, T0, portfolio, MARKS)
        assert fill.fee > 0
        return portfolio

    portfolio = run(fake, tmp_path, action, stop_pct=0.0)
    # A buy pays its commission in the coin bought: the book holds what the account holds.
    assert portfolio.position(BTC) == pytest.approx(fake.balances["BTC"])


def test_two_sessions_on_one_account_keep_their_stops(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.2}, prices={BTC: 50000.0})

    async def go() -> None:
        async with fake.client() as client:
            spot = BinanceSpot(
                client, fake.api_key, fake.secret, base_url=TESTNET_URL, clock=lambda: T0
            )
            await spot.load_rules([BTC])
            sessions = []
            for tag in ("aaaa", "bbbb"):
                ledger = Ledger(tmp_path / f"{tag}.sqlite")
                ledger.set_meta(TAG_META, tag)
                executor = LiveExecutor(
                    spot, ledger, Collect(), lambda: T0, fee_rate=0.001, protective_stop_pct=0.2
                )
                portfolio = Portfolio(0.0)
                portfolio.apply(Fill(T0, BTC, 0.1, 50000.0, 0.0))
                sessions.append((executor, portfolio, ledger))
            for executor, portfolio, _ in sessions * 2:  # each replaces its stop twice
                await executor.after_event(portfolio, MARKS)
                assert not executor.unprotected
            for _, _, ledger in sessions:
                ledger.close()

    asyncio.run(go())
    stops = [o for o in fake.orders if o["type"] == "STOP_LOSS_LIMIT" and o["status"] == "NEW"]
    assert sorted(o["clientOrderId"][:7] for o in stops) == ["tbsaaaa", "tbsbbbb"]


def test_a_float_a_hair_below_a_step_rounds_to_it() -> None:
    step = Decimal("0.0001")
    rules = SymbolRules("ETH", "USDT", Decimal("0.01"), step, step, Decimal("5"))
    assert rules.round_quantity(0.7 + 0.1) == 0.8  # 0.7999999999999999
    assert rules.round_quantity(-(0.7 + 0.1)) == -0.8
    assert rules.round_quantity(0.79995) == 0.7999  # a real remainder is still cut
    assert rules.round_quantity(0.00009) == 0.0


def test_coins_bought_while_a_stop_fills_are_flagged(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.1}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> set[str]:
        portfolio = Portfolio(0.0)
        portfolio.apply(Fill(T0, BTC, 0.1, 50000.0, 0.0))
        await executor.after_event(portfolio, MARKS)  # stop 40000 for 0.1 BTC
        stop = next(o for o in fake.orders if o["type"] == "STOP_LOSS_LIMIT")
        stop.update(executedQty="0.03000000", cummulativeQuoteQty=f"{0.03 * 39800:.8f}")
        fake.locked["BTC"] -= 0.03
        fake.balances["USDT"] = 0.03 * 39800
        assert await executor.settle(portfolio) == 1
        portfolio.apply(Fill(T0, BTC, 0.05, 39800.0, 0.0))  # bought while it still fills
        fake.balances["BTC"] += 0.05
        await executor.after_event(portfolio, MARKS)
        assert stop["status"] == "NEW"
        assert "0.050000 bought since has no exchange stop" in notifier.messages[-1]
        return executor.unprotected

    assert run(fake, tmp_path, action) == {BTC}


def test_a_position_whose_coins_are_locked_is_not_dust(tmp_path: Path) -> None:
    # The owner's limit order locks all but 0.00005 BTC of the 0.5 the bot holds.
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.00005}, prices={BTC: 50000.0})
    fake.locked["BTC"] = 0.49995

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> set[str]:
        portfolio = Portfolio(0.0)
        portfolio.apply(Fill(T0, BTC, 0.5, 50000.0, 0.0))
        await executor.after_event(portfolio, MARKS)
        assert "only 0.00005000 of the 0.50000000 held is free" in notifier.messages[-1]
        return executor.unprotected

    assert run(fake, tmp_path, action) == {BTC}
    assert fake.order_count("STOP_LOSS_LIMIT") == 0


def test_a_lowered_budget_stays_lowered(tmp_path: Path) -> None:
    # Budget 1000 with 996 in BTC, lowered to 500: the book owes 496 until it sells.
    fake = FakeSpot(balances={"USDT": 4.0, "BTC": 0.01992}, prices={BTC: 50000.0})
    config = _live_config(tmp_path)

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> Portfolio:
        note = f"{BUDGET_NOTE} from 1,000.00 to 500.00"
        ledger.add_adjustment(Adjustment(T0, "", 0.0, -500.0, note))
        portfolio = Portfolio(1000.0)
        portfolio.apply(Fill(T0, BTC, 0.01992, 50000.0, 0.0))
        portfolio.adjust("", 0.0, -500.0)
        guard = RiskGuard(GuardConfig())
        await reconcile_round(
            executor, portfolio, MARKS, ledger, notifier, guard, config, lambda: T0
        )
        return portfolio

    portfolio = run(fake, tmp_path, action, stop_pct=0.0)
    assert portfolio.cash == pytest.approx(-496.0)  # not handed back as a deposit


def test_a_paused_symbol_gets_no_orders_until_binance_trades_it_again(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})
    fake.statuses[BTC] = "BREAK"  # maintenance, or a pair on its way out

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> list[str]:
        portfolio = Portfolio(1000.0)
        await executor.report_status()
        await executor.report_status()  # said once
        assert await executor.execute({BTC: 0.01}, T0, portfolio, MARKS) == []
        await executor.after_event(portfolio, MARKS)
        fake.statuses.clear()
        executor.spot.rules_time = float("-inf")  # due for a reload
        await executor.check_rules()
        assert len(await executor.execute({BTC: 0.01}, T0, portfolio, MARKS)) == 1
        return statuses(ledger)

    assert run(fake, tmp_path, action) == ["skipped", "pending", "filled"]
    assert fake.order_count("MARKET") == 1


def test_paused_symbol_alerts(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})
    fake.statuses[BTC] = "BREAK"

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> list[str]:
        await executor.report_status()
        fake.statuses.clear()
        executor.spot.rules_time = float("-inf")
        await executor.check_rules()
        await executor.check_rules()
        return notifier.messages

    assert run(fake, tmp_path, action) == [
        "[live] Binance does not trade BTCUSDT now (BREAK): no orders or stops for it until it "
        "does",
        "[live] Binance trades BTCUSDT again; its orders and stops resume",
    ]


def test_reconciliation_reloads_the_rules_after_a_filter_failure(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.01234}, prices={BTC: 50000.0})
    config = _live_config(tmp_path)

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> Decimal:
        fake.rules[BTC] = {**fake.rules[BTC], "step": "0.001", "minQty": "0.001"}
        fake.fail_next = [httpx.Response(400, json={"code": -1013, "msg": "LOT_SIZE"})]
        portfolio = Portfolio(0.0)
        portfolio.apply(Fill(T0, BTC, 0.01234, 50000.0, 0.0))
        await executor.after_event(portfolio, MARKS)  # the stop fails the new step size
        assert executor.unprotected == {BTC}
        guard = RiskGuard(GuardConfig())
        await reconcile_round(
            executor, portfolio, MARKS, ledger, notifier, guard, config, lambda: T0
        )
        assert executor.unprotected == set()  # placed on the reloaded rules
        return executor.spot.rules[BTC].step_size

    assert run(fake, tmp_path, action) == Decimal("0.001")
    [stop] = [o for o in fake.orders if o["type"] == "STOP_LOSS_LIMIT"]
    assert float(stop["origQty"]) == pytest.approx(0.012)


def test_a_sell_never_asks_for_more_than_is_free(tmp_path: Path) -> None:
    doge = "DOGEUSDT"
    fake = FakeSpot(
        balances={"USDT": 0.0, "DOGE": 99.99999999},  # a hair under the 100 booked
        prices={doge: 0.1},
        rules={
            **fake_rules(),
            doge: {"tick": "0.00001", "step": "1", "minQty": "1", "minNotional": "1"},
        },
    )

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> list[Fill]:
        await executor.spot.load_rules([doge])
        portfolio = Portfolio(0.0)
        portfolio.apply(Fill(T0, doge, 100.0, 0.1, 0.0))
        return await executor.execute({doge: -100.0}, T0, portfolio, {doge: 0.1})

    [fill] = run(fake, tmp_path, action, stop_pct=0.0)
    assert fill.quantity == pytest.approx(-99.0)  # not 100, which Binance would refuse


def fake_rules() -> dict[str, dict[str, str]]:
    return dict(FakeSpot(balances={}, prices={}).rules)


def test_a_stop_over_part_of_the_position_says_so_once(tmp_path: Path) -> None:
    # The owner's order locks 0.4 of the 0.5 BTC the bot holds.
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.1}, prices={BTC: 50000.0})
    fake.locked["BTC"] = 0.4

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> list[str]:
        portfolio = Portfolio(0.0)
        portfolio.apply(Fill(T0, BTC, 0.5, 50000.0, 0.0))
        await executor.after_event(portfolio, MARKS)
        await executor.after_event(portfolio, MARKS)
        assert executor.partial == {BTC: pytest.approx(0.4)}
        assert executor.unprotected == set()  # the stop placed is not replaced every round
        return notifier.messages

    assert run(fake, tmp_path, action) == [
        "[live] BTCUSDT: the stop covers 0.100000 of 0.500000; other orders lock the rest, "
        "which has no exchange stop"
    ]


def test_a_budget_cut_the_cash_covered_leaves_nothing_owed(tmp_path: Path) -> None:
    # 1000 in cash, budget lowered to 500: nothing is owed, so a book that goes negative
    # later (a fill booked twice) is set back to zero, as before budgets could owe.
    fake = FakeSpot(balances={"USDT": 600.0}, prices={BTC: 50000.0})
    config = _live_config(tmp_path).model_copy(update={"initial_cash": 500.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> float:
        ledger.set_meta(BUDGET_META, "1000.0")
        guard = RiskGuard(GuardConfig())
        check_budget(config, ledger, guard, T0)
        assert budget_owed(ledger) == 0.0
        portfolio = Portfolio(500.0)
        portfolio.adjust("", 0.0, -800.0)  # the bug that books cash twice
        await reconcile_round(
            executor, portfolio, MARKS, ledger, notifier, guard, config, lambda: T0
        )
        return portfolio.cash

    assert run(fake, tmp_path, action, stop_pct=0.0) == pytest.approx(0.0)


def test_what_a_budget_cut_owes_shrinks_as_sales_repay_it(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 4.0, "BTC": 0.01992}, prices={BTC: 50000.0})
    config = _live_config(tmp_path).model_copy(update={"initial_cash": 500.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> list[float]:
        portfolio = Portfolio(1000.0)
        fill = Fill(T0, BTC, 0.01992, 50000.0, 0.0)
        portfolio.apply(fill)
        ledger.add_fill(fill, 50000.0)
        ledger.set_meta(BUDGET_META, "1000.0")
        guard = RiskGuard(GuardConfig())
        check_budget(config, ledger, guard, T0)  # takes 500 from the 4 the book holds
        portfolio.adjust("", 0.0, -500.0)
        owed = [budget_owed(ledger)]
        portfolio.adjust("", 0.0, 296.0)  # a sale repaid 296 of it
        fake.balances["USDT"] = 300.0
        await reconcile_round(
            executor, portfolio, MARKS, ledger, notifier, guard, config, lambda: T0
        )
        owed.append(float(ledger.get_meta(OWED_META) or "nan"))
        return owed

    assert run(fake, tmp_path, action, stop_pct=0.0) == [-496.0, -200.0]


def test_a_paused_symbol_keeps_its_stop_as_it_is(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.01}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> list[str]:
        portfolio = Portfolio(0.0)
        portfolio.apply(Fill(T0, BTC, 0.01, 50000.0, 0.0))
        await executor.after_event(portfolio, MARKS)  # a stop while it trades
        fake.statuses[BTC] = "BREAK"
        executor.spot.rules_time = float("-inf")
        await executor.check_rules()
        await executor.after_event(portfolio, MARKS)  # not replaced while paused
        await executor.protect(portfolio, MARKS)
        return [o["status"] for o in fake.orders if o["type"] == "STOP_LOSS_LIMIT"]

    assert run(fake, tmp_path, action) == ["NEW"]  # neither cancelled nor placed again


def test_a_stop_never_asks_for_more_than_is_free(tmp_path: Path) -> None:
    doge = "DOGEUSDT"
    fake = FakeSpot(
        balances={"USDT": 0.0, "DOGE": 99.99999999},
        prices={doge: 0.1},
        rules={
            **fake_rules(),
            doge: {"tick": "0.00001", "step": "1", "minQty": "1", "minNotional": "1"},
        },
    )

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> set[str]:
        await executor.spot.load_rules([doge])
        portfolio = Portfolio(0.0)
        portfolio.apply(Fill(T0, doge, 100.0, 0.1, 0.0))
        await executor.after_event(portfolio, {doge: 0.1})
        return executor.unprotected

    assert run(fake, tmp_path, action) == set()
    [stop] = [o for o in fake.orders if o["type"] == "STOP_LOSS_LIMIT"]
    assert float(stop["origQty"]) == 99.0


def test_a_paper_quote_lost_to_a_blip_is_asked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []

    async def no_wait(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr("tbot.live.executor.asyncio.sleep", no_wait)
    answers: list[httpx.Response | Exception] = [httpx.ConnectError("down"), httpx.Response(503)]

    def handle(request: httpx.Request) -> httpx.Response:
        answer = answers.pop(0) if answers else None
        if isinstance(answer, Exception):
            raise answer
        return answer or httpx.Response(200, json={"askPrice": "101.5", "bidPrice": "101.0"})

    async def quote() -> float:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await BinanceBookTicker(client).price(BTC, 1)

    assert asyncio.run(quote()) == 101.5
    assert waits == [1, 2]
