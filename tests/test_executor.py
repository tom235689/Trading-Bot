import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from binance_spot_fake import FakeSpot, symbol_info
from tbot.core.config import StrategyConfig
from tbot.core.models import Fill
from tbot.core.timeframe import Timeframe
from tbot.exchange.binance import (
    NOTHING_TO_CANCEL,
    TESTNET_URL,
    Balance,
    BinanceError,
    BinanceSpot,
    Order,
    SymbolRules,
    parse_rules,
)
from tbot.live.config import LiveConfig
from tbot.live.executor import (
    ORDER_PATIENCE,
    TAG_META,
    BinanceBookTicker,
    LiveExecutor,
    order_id,
)
from tbot.live.ledger import Adjustment, EquityPoint, Ledger
from tbot.live.reconcile import reconcile
from tbot.live.runner import (
    BUDGET_META,
    BUDGET_NOTE,
    GUARD_META,
    OWED_META,
    budget_owed,
    check_budget,
    final_protect,
    quote_problem,
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
    [stop] = [o for o in fake.orders if o["type"] == "STOP_LOSS"]
    assert (stop["status"], stop["origQty"]) == ("NEW", "0.00999000")
    assert (stop["stopPrice"], stop["price"]) == ("40000.00", "0")  # stop-market: no limit


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
        ("STOP_LOSS", "CANCELED"),
        ("MARKET", "FILLED"),
        ("STOP_LOSS", "NEW"),
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
    assert fake.order_count("STOP_LOSS") == 0


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
        assert fake.order_count("STOP_LOSS") == 1

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

        async def unreachable(
            symbol: str, client_id: str, attempts: int | None = None
        ) -> Order | None:
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
        assert [r.client_id for r in ledger.unresolved_orders()] == [lost]  # may show up late
        executor.clock = lambda: T0 + timedelta(seconds=ORDER_PATIENCE)
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

        [stop] = [o for o in fake.orders if o["type"] == "STOP_LOSS"]
        await executor.spot.cancel_order(BTC, stop["orderId"])  # the owner cancels it
        assert await executor.settle(portfolio) == 0
        assert executor.unprotected == {BTC}
        await executor.protect(portfolio, MARKS)
        return notifier

    notifier = run(fake, tmp_path, action)
    assert sum(o["status"] == "NEW" for o in fake.orders if o["type"] == "STOP_LOSS") == 1
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
        executor.clock = lambda: T0 + timedelta(seconds=ORDER_PATIENCE - 1)  # since it was sent
        assert await executor.settle(Portfolio(1000.0)) == 0
        assert statuses(ledger) == ["pending", "unknown"]  # Binance may list it late
        executor.clock = lambda: T0 + timedelta(seconds=ORDER_PATIENCE)
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

        async def unreachable(
            symbol: str, client_id: str, attempts: int | None = None
        ) -> Order | None:
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
    stops = [o for o in fake.orders if o["type"] == "STOP_LOSS" and o["status"] == "NEW"]
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

        async def unreachable(
            symbol: str, client_id: str, attempts: int | None = None
        ) -> Order | None:
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

        async def unreachable(
            symbol: str, client_id: str, attempts: int | None = None
        ) -> Order | None:
            raise httpx.ConnectError("down")

        monkeypatch.setattr(executor.spot, "get_order", unreachable)
        await executor.execute({BTC: -0.01}, T0, portfolio, MARKS)
        await executor.after_event(portfolio, MARKS)  # the book still says 0.01
        return executor.unprotected

    assert run(fake, tmp_path, action) == {BTC}  # placed after reconciliation settles it
    assert fake.order_count("STOP_LOSS") == 0  # not on the owner's coins


def test_a_partly_filled_stop_keeps_working(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.1}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> Portfolio:
        portfolio = Portfolio(0.0)
        portfolio.apply(Fill(T0, BTC, 0.1, 50000.0, 0.0))
        await executor.after_event(portfolio, MARKS)  # stop 40000 for 0.1 BTC
        stop = next(o for o in fake.orders if o["type"] == "STOP_LOSS")
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
    assert fake.order_count("STOP_LOSS") == 1


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
    stops = [o for o in fake.orders if o["type"] == "STOP_LOSS" and o["status"] == "NEW"]
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
        stop = next(o for o in fake.orders if o["type"] == "STOP_LOSS")
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
    assert fake.order_count("STOP_LOSS") == 0


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
    [stop] = [o for o in fake.orders if o["type"] == "STOP_LOSS"]
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
        assert budget_owed(ledger) == -500.0  # until the start's reconciliation knows better
        portfolio = Portfolio(500.0)
        await reconcile_round(
            executor, portfolio, MARKS, ledger, notifier, guard, config, lambda: T0
        )
        assert budget_owed(ledger) == 0.0
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

    assert run(fake, tmp_path, action, stop_pct=0.0) == [-500.0, -200.0]


def test_a_budget_cut_with_a_buy_in_doubt_takes_none_of_the_owners_cash(tmp_path: Path) -> None:
    # A buy sent before a crash executed but was never booked; the restart lowers the
    # budget from 1000 to 500. Booking the buy leaves the book owing 490: the owner's 4000
    # USDT must not pay it back.
    fake = FakeSpot(balances={"USDT": 5000.0}, prices={BTC: 50000.0})
    config = _live_config(tmp_path).model_copy(update={"initial_cash": 500.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> list[float]:
        ledger.add_order(T0, BTC, 0.0198, "pending", "", "tb-crash")
        await executor.spot.market_order(BTC, 0.0198, "tb-crash")  # its answer was lost
        ledger.set_meta(BUDGET_META, "1000.0")
        guard = RiskGuard(GuardConfig())
        check_budget(config, ledger, guard, T0)
        portfolio = Portfolio(1000.0)
        portfolio.adjust("", 0.0, -500.0)  # the cut, as the restored book has it
        await reconcile_round(
            executor, portfolio, MARKS, ledger, notifier, guard, config, lambda: T0
        )
        return [portfolio.cash, budget_owed(ledger)]

    cash, owed = run(fake, tmp_path, action, stop_pct=0.0)
    assert cash == pytest.approx(-490.099, abs=0.01)
    assert owed == pytest.approx(cash)


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
        return [o["status"] for o in fake.orders if o["type"] == "STOP_LOSS"]

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
    [stop] = [o for o in fake.orders if o["type"] == "STOP_LOSS"]
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


def test_an_order_refused_for_outdated_rules_goes_out_again_on_the_new_ones(
    tmp_path: Path,
) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> list[Fill]:
        fake.rules[BTC] = {**fake.rules[BTC], "step": "0.001", "minQty": "0.001"}
        fills = await executor.execute({BTC: 0.01234}, T0, Portfolio(1000.0), MARKS)
        assert statuses(ledger) == ["pending", "failed", "pending", "filled"]
        return fills

    [fill] = run(fake, tmp_path, action, stop_pct=0.0)
    assert fill.quantity == pytest.approx(0.012 * 0.999)  # in the bar, on the new step


def test_order_lookups_after_a_lost_answer_are_single_tries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    waits: list[float] = []

    async def no_wait(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr("tbot.exchange.binance.asyncio.sleep", no_wait)
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})
    fake.lose_next_order_response = True
    original = fake.handle
    lookups = 0

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal lookups
        if request.method == "GET" and request.url.path == "/api/v3/order":
            lookups += 1
            if lookups <= 3:  # every lookup the executor makes (LOOKUP_DELAYS)
                raise httpx.ConnectError("down")
        return original(request)

    fake.handle = flaky  # type: ignore[method-assign]

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> list[str]:
        await executor.execute({BTC: 0.01}, T0, Portfolio(1000.0), MARKS)
        return statuses(ledger)

    assert run(fake, tmp_path, action, stop_pct=0.0) == ["pending", "unknown"]
    assert lookups == 3  # the lookups are the retries: none retries itself under the lock
    assert [w for w in waits if w >= 1] == []


def test_a_resumed_pair_gets_its_stop_refitted(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})
    fake.statuses[BTC] = "BREAK"

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> set[str]:
        await executor.report_status()
        fake.statuses.clear()
        executor.spot.rules_time = float("-inf")
        await executor.check_rules()
        return executor.unprotected

    assert run(fake, tmp_path, action) == {BTC}  # reconciliation fits it to the book


def test_a_stop_that_could_not_be_replaced_says_the_old_one_stays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.01}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> list[str]:
        portfolio = Portfolio(0.0)
        portfolio.apply(Fill(T0, BTC, 0.01, 50000.0, 0.0))
        await executor.after_event(portfolio, MARKS)  # a stop is in place

        async def down(symbol: str) -> list[Order]:
            raise httpx.ConnectError("down")

        monkeypatch.setattr(executor.spot, "open_orders", down)
        executor.partial[BTC] = 0.004  # from an earlier stop over part of the position
        await executor.after_event(portfolio, MARKS)
        assert executor.partial == {}
        return notifier.messages

    messages = run(fake, tmp_path, action)
    assert "The stop in place, if any, was not replaced and stays" in messages[-1]
    assert [o["status"] for o in fake.orders if o["type"] == "STOP_LOSS"] == ["NEW"]


def test_a_stop_that_executed_while_the_bot_was_down_is_booked_at_its_time(
    tmp_path: Path,
) -> None:
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.01}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> list[Fill]:
        portfolio = Portfolio(0.0)
        portfolio.apply(Fill(T0, BTC, 0.01, 50000.0, 0.0))
        await executor.after_event(portfolio, MARKS)
        fake.trigger_stop(BTC, at=T0 - timedelta(hours=2))
        await executor.settle(portfolio)
        return ledger.fills()

    assert run(fake, tmp_path, action)[-1].time == T0 - timedelta(hours=2)


def test_a_ledger_from_before_owes_what_its_cuts_took(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 0.0}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> float:
        for cash in (1000.0, -1500.0):  # raised and spent, then cut (0.4.2 kept no debt)
            note = f"{BUDGET_NOTE} by {cash}"
            ledger.add_adjustment(Adjustment(T0, "", 0.0, cash, note))
        return budget_owed(ledger)

    assert run(fake, tmp_path, action) == -1500.0  # never less than the book may owe


def test_a_stop_is_a_stop_market_order_binance_takes_far_under_the_price(
    tmp_path: Path,
) -> None:
    # A stop-limit sell priced under 0.8x the average price fails PERCENT_PRICE_BY_SIDE.
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.01}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> set[str]:
        portfolio = Portfolio(0.0)
        portfolio.apply(Fill(T0, BTC, 0.01, 50000.0, 0.0))
        await executor.after_event(portfolio, MARKS)
        return executor.unprotected

    assert run(fake, tmp_path, action, stop_pct=0.3) == set()
    [stop] = [o for o in fake.orders if o["status"] == "NEW"]
    assert (stop["type"], stop["stopPrice"]) == ("STOP_LOSS", "35000.00")
    [placed] = [r for r in fake.requests if r.method == "POST"]
    assert b"price=" not in placed.url.query.replace(b"stopPrice=", b"")
    assert b"timeInForce" not in placed.url.query


def test_a_buy_booked_by_a_failed_round_gets_its_stop_at_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})
    fake.error_after_next_order = httpx.Response(503)  # filled, but the answer is lost

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> Portfolio:
        portfolio = Portfolio(1000.0)
        lookup, balances = executor.spot.get_order, executor.spot.balances

        async def unreachable(
            symbol: str, client_id: str, attempts: int | None = None
        ) -> Order | None:
            raise httpx.ConnectError("down")

        monkeypatch.setattr(executor.spot, "get_order", unreachable)
        await executor.execute({BTC: 0.01}, T0, portfolio, MARKS)
        await executor.after_event(portfolio, MARKS)  # the book holds nothing yet: no stop
        monkeypatch.setattr(executor.spot, "get_order", lookup)

        async def blip() -> dict[str, Balance]:
            raise httpx.ConnectError("blip")

        monkeypatch.setattr(executor.spot, "balances", blip)
        guard, config = RiskGuard(GuardConfig()), _live_config(tmp_path)
        with pytest.raises(httpx.ConnectError):  # settle booked the buy, then a blip
            await reconcile_round(
                executor, portfolio, MARKS, ledger, notifier, guard, config, lambda: T0
            )
        monkeypatch.setattr(executor.spot, "balances", balances)
        await reconcile_round(
            executor, portfolio, MARKS, ledger, notifier, guard, config, lambda: T0
        )
        return portfolio

    portfolio = run(fake, tmp_path, action)
    assert portfolio.position(BTC) == pytest.approx(0.00999)
    stops = [o for o in fake.orders if o["type"] == "STOP_LOSS" and o["status"] == "NEW"]
    assert [float(o["origQty"]) for o in stops] == [pytest.approx(0.00999)]


def test_a_stop_cancelled_before_a_failed_lookup_is_said_to_be_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.01}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> list[str]:
        portfolio = Portfolio(0.0)
        portfolio.apply(Fill(T0, BTC, 0.01, 50000.0, 0.0))
        await executor.after_event(portfolio, MARKS)  # a stop is in place

        async def down(symbol: str, client_id: str, attempts: int | None = None) -> Order | None:
            raise BinanceError(0, "Service Unavailable", 503)

        monkeypatch.setattr(executor.spot, "get_order", down)
        await executor.after_event(portfolio, MARKS)  # cancelled, then its lookup fails
        return notifier.messages

    messages = run(fake, tmp_path, action)
    assert [o["status"] for o in fake.orders if o["type"] == "STOP_LOSS"] == ["CANCELED"]
    assert "The position has no exchange stop" in messages[-1]


def test_a_pair_binance_stops_listing_leaves_the_others_checked(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> list[str]:
        del fake.rules["ETHUSDT"]  # delisted: Binance refuses any list that names it
        fake.statuses[BTC] = "BREAK"
        executor.spot.rules_time = float("-inf")
        await executor.check_rules()
        assert executor.spot.status == {BTC: "BREAK", "ETHUSDT": "UNLISTED"}
        assert not executor.spot.trading("ETHUSDT")
        assert "ETHUSDT" in executor.spot.rules  # its book still prices
        return notifier.messages

    messages = run(fake, tmp_path, action)
    assert any("BTCUSDT now (BREAK)" in m for m in messages)
    assert any("ETHUSDT now (UNLISTED)" in m for m in messages)

    async def start() -> None:
        async with fake.client() as client:
            spot = BinanceSpot(client, fake.api_key, fake.secret, base_url=TESTNET_URL)
            await spot.load_rules([BTC, "ETHUSDT"])

    with pytest.raises(BinanceError, match="Binance does not list ETHUSDT"):
        asyncio.run(start())


def test_a_budget_change_after_a_withdrawal_scales_from_what_is_left(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 600.0}, prices={BTC: 50000.0})  # 400 withdrawn
    config = _live_config(tmp_path)

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> RiskGuard:
        portfolio = Portfolio(1000.0)
        ledger.add_equity(EquityPoint(T0, 1000.0, 1000.0, 0.0))  # the last bar event
        ledger.set_meta(BUDGET_META, "1000.0")
        guard = RiskGuard(GuardConfig())
        guard.check(T0, 1000.0, T0)
        await reconcile_round(
            executor, portfolio, MARKS, ledger, notifier, guard, config, lambda: T0
        )
        lower = config.model_copy(update={"initial_cash": 700.0})  # then a restart: -300
        check_budget(lower, ledger, guard, T0)
        assert guard.state.peak_equity == pytest.approx(600.0)  # rescaled once settled
        portfolio.adjust("", 0.0, -300.0)  # the restored book has the cut
        await reconcile_round(
            executor, portfolio, MARKS, ledger, notifier, guard, lower, lambda: T0
        )
        return guard

    guard = run(fake, tmp_path, action, stop_pct=0.0)
    assert guard.state.peak_equity == pytest.approx(300.0)  # 600 left, then 300 taken


def stops(fake: FakeSpot) -> list[tuple[str, str]]:
    return [(o["stopPrice"], o["status"]) for o in fake.orders if o["type"] == "STOP_LOSS"]


def test_a_stop_stays_when_the_price_fell_through_the_new_level(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.01}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> list[str]:
        portfolio = Portfolio(0.0)
        portfolio.apply(Fill(T0, BTC, 0.01, 50000.0, 0.0))
        await executor.after_event(portfolio, MARKS)  # a stop at 40,000
        fake.prices[BTC] = 47000.0  # down from a close at 60,000 since
        await executor.after_event(portfolio, {BTC: 60000.0})  # 48,000 would trigger at once
        assert executor.unprotected == set()
        return notifier.messages

    assert run(fake, tmp_path, action) == []
    assert stops(fake) == [("40000.00", "NEW")]


def test_a_missing_stop_goes_below_the_price_when_it_fell_through_the_level(
    tmp_path: Path,
) -> None:
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.01}, prices={BTC: 47000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> None:
        portfolio = Portfolio(0.0)
        portfolio.apply(Fill(T0, BTC, 0.01, 50000.0, 0.0))
        await executor.after_event(portfolio, {BTC: 60000.0})
        assert executor.unprotected == set()

    run(fake, tmp_path, action)
    [(stop, status)] = stops(fake)
    assert status == "NEW"
    assert float(stop) == pytest.approx(47000.0 * 0.9999 * 0.8, abs=0.01)  # 20% under the bid


def test_a_stop_a_failed_sale_cancelled_is_said_to_be_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.01}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> list[str]:
        portfolio = Portfolio(0.0)
        portfolio.apply(Fill(T0, BTC, 0.01, 50000.0, 0.0))
        await executor.after_event(portfolio, MARKS)  # a stop is in place

        async def limited(*args: object, **kwargs: object) -> Order:
            async def down(symbol: str) -> list[Order]:
                raise BinanceError(-1003, "Too many requests.", 429)

            monkeypatch.setattr(executor.spot, "open_orders", down)  # paused from now on
            raise BinanceError(-1003, "Too many requests.", 429)

        monkeypatch.setattr(executor.spot, "market_order", limited)
        assert await executor.execute({BTC: -0.01}, T0, portfolio, MARKS) == []
        await executor.after_event(portfolio, MARKS)
        return notifier.messages

    messages = run(fake, tmp_path, action)
    assert stops(fake) == [("40000.00", "CANCELED")]
    [alert] = [m for m in messages if "protective stop failed" in m]  # none for ETH, not held
    assert alert.startswith("[live] protective stop failed for BTCUSDT")
    assert "The position has no exchange stop" in alert


def test_turning_stops_off_cancels_the_one_placed_before(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.01}, prices={BTC: 50000.0})

    def portfolio() -> Portfolio:
        book = Portfolio(0.0)
        book.apply(Fill(T0, BTC, 0.01, 50000.0, 0.0))
        return book

    async def place(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> None:
        await executor.after_event(portfolio(), MARKS)

    async def off(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> list[str]:
        book = portfolio()
        await executor.after_event(book, MARKS)
        await executor.after_event(book, MARKS)
        return notifier.messages

    run(fake, tmp_path, place)
    messages = run(fake, tmp_path, off, stop_pct=0.0)
    assert stops(fake) == [("40000.00", "CANCELED")]  # it would sell the coins later
    assert fake.locked["BTC"] == pytest.approx(0.0)
    assert [m for m in messages if "protective stops are off" in m] == [
        "[live] protective stops are off (protective_stop_pct 0): cancelled the exchange "
        "stop of BTCUSDT"
    ]


def test_the_largest_order_is_the_smaller_of_both_lot_limits() -> None:
    lot = {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001"}
    market = {"filterType": "MARKET_LOT_SIZE", "stepSize": "0", "minQty": "0", "maxQty": "85.5"}
    others = [
        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
        {"filterType": "NOTIONAL", "minNotional": "5"},
    ]

    def rules(*filters: dict[str, str]) -> SymbolRules:
        return parse_rules(
            {"baseAsset": "BTC", "quoteAsset": "USDT", "filters": [*others, *filters]}
        )

    capped = rules({**lot, "maxQty": "9000"}, market)
    assert capped.cap_quantity(-100.0) == -85.5
    assert capped.cap_quantity(2.0) == 2.0
    assert rules(lot).cap_quantity(1e6) == 1e6  # no limit given


def test_a_market_order_above_the_exchange_maximum_sells_what_it_may(tmp_path: Path) -> None:
    rules = fake_rules()
    rules[BTC] = {**rules[BTC], "marketMaxQty": "1.00000"}
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 3.0}, prices={BTC: 50000.0}, rules=rules)

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> Portfolio:
        portfolio = Portfolio(0.0)
        portfolio.positions[BTC] = 3.0
        [fill] = await executor.execute({BTC: -3.0}, T0, portfolio, MARKS)
        assert fill.quantity == pytest.approx(-1.0)  # the rest at the next decision
        return portfolio

    assert run(fake, tmp_path, action, stop_pct=0.0).position(BTC) == pytest.approx(2.0)


def test_a_stop_binance_archived_is_forgotten_and_placed_again(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.01}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> int:
        portfolio = Portfolio(0.0)
        portfolio.apply(Fill(T0, BTC, 0.01, 50000.0, 0.0))
        await executor.after_event(portfolio, MARKS)
        [stop] = [o for o in fake.orders if o["type"] == "STOP_LOSS"]
        fake._cancel(stop)  # cancelled by hand while the bot was down for months
        fake.archived.add(stop["clientOrderId"])
        booked = await executor.settle(portfolio)  # raised -2026 at every round before
        assert executor.unprotected == {BTC}
        await executor.protect(portfolio, MARKS)
        return booked

    assert run(fake, tmp_path, action) == 0
    assert [o["status"] for o in fake.orders] == ["CANCELED", "NEW"]


def test_an_order_in_doubt_binance_archived_ended_unfilled(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> list[str]:
        sent = order_id(T0, BTC, 1.0)
        ledger.add_order(T0, BTC, 0.01, "pending", "", sent)  # left by a crash long ago
        fake.archived.add(sent)
        await executor.settle(Portfolio(1000.0))
        assert ledger.unresolved_orders() == []
        return notifier.messages

    assert "ended unfilled (ARCHIVED); nothing was traded" in run(fake, tmp_path, action)[-1]


def test_a_new_largest_order_alone_leaves_the_stops_as_they_are(tmp_path: Path) -> None:
    rules = fake_rules()
    rules[BTC] = {**rules[BTC], "marketMaxQty": "121.36728479"}
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.01}, prices={BTC: 50000.0}, rules=rules)

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> set[str]:
        portfolio = Portfolio(0.0)
        portfolio.apply(Fill(T0, BTC, 0.01, 50000.0, 0.0))
        await executor.after_event(portfolio, MARKS)
        rules[BTC]["marketMaxQty"] = "122.71068933"  # as Binance moves it within the hour
        executor.spot.rules_time = float("-inf")
        await executor.check_rules()
        assert executor.spot.rules[BTC].max_qty == Decimal("122.71068933")
        return executor.unprotected

    assert run(fake, tmp_path, action) == set()
    assert fake.order_count("STOP_LOSS") == 1


def test_a_market_order_binance_ends_before_it_is_done_is_told(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.5}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> list[str]:
        portfolio = Portfolio(0.0)
        portfolio.positions[BTC] = 0.5
        fake.market_fraction = 0.0  # nothing on the other side of the book
        assert await executor.execute({BTC: -0.5}, T0, portfolio, MARKS) == []
        fake.market_fraction = 0.2
        [fill] = await executor.execute({BTC: -0.5}, T0 + timedelta(hours=4), portfolio, MARKS)
        assert fill.quantity == pytest.approx(-0.1)
        assert portfolio.position(BTC) == pytest.approx(0.4)
        return notifier.messages

    messages = run(fake, tmp_path, action, stop_pct=0.0)
    assert messages[0] == (
        "[live] Binance ended the SELL order for BTCUSDT (EXPIRED) with nothing executed; "
        "it waits for the next decision"
    )
    assert messages[1] == (
        "[live] Binance ended the SELL order for BTCUSDT (EXPIRED) after 0.100000 of "
        "0.500000; the rest waits for the next decision"
    )
    assert messages[2].startswith("[live] SELL 0.100000 BTCUSDT")


def test_an_order_whose_answer_says_unknown_status_is_looked_up(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})
    fake.error_after_next_order = httpx.Response(  # executed, but Binance cannot say so
        400, json={"code": -1006, "msg": "An unexpected response was received."}
    )

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> Portfolio:
        portfolio = Portfolio(1000.0)
        assert len(await executor.execute({BTC: 0.01}, T0, portfolio, MARKS)) == 1
        assert statuses(ledger) == ["pending", "filled"]
        return portfolio

    portfolio = run(fake, tmp_path, action, stop_pct=0.0)
    assert portfolio.position(BTC) == pytest.approx(fake.balances["BTC"])  # not lost


def test_a_stop_above_the_largest_order_covers_what_binance_takes(tmp_path: Path) -> None:
    rules = fake_rules()
    rules[BTC] = {**rules[BTC], "marketMaxQty": "1.00000"}
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 3.0}, prices={BTC: 50000.0}, rules=rules)
    fake.stop_max_qty = 1.0

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> list[str]:
        portfolio = Portfolio(0.0)
        portfolio.apply(Fill(T0, BTC, 3.0, 50000.0, 0.0))
        await executor.after_event(portfolio, MARKS)
        assert executor.unprotected == set()
        assert executor.partial == {BTC: pytest.approx(2.0)}
        return notifier.messages

    messages = run(fake, tmp_path, action)
    assert [o["origQty"] for o in fake.orders if o["status"] == "NEW"] == ["1.00000000"]
    assert messages == [
        "[live] BTCUSDT: the stop covers 1.000000 of 3.000000; Binance takes no larger order, "
        "so the rest has no exchange stop"
    ]


def test_a_market_binance_closed_reloads_the_pair_status(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> list[str]:
        fake.statuses[BTC] = "BREAK"  # paused since the last load of the rules
        fake.fail_next.append(httpx.Response(400, json={"code": -2010, "msg": "Market is closed."}))
        with pytest.raises(BinanceError):
            await executor.spot.stop_loss_order(BTC, 0.01, 40000.0, "tbsx")
        await executor.check_rules()
        return notifier.messages

    assert "Binance does not trade BTCUSDT now (BREAK)" in run(fake, tmp_path, action)[-1]


def test_an_order_in_doubt_is_booked_when_it_executed(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0}, now=T0)

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> Fill:
        sent = order_id(T0, BTC, 1.0)
        ledger.add_order(T0, BTC, 0.01, "pending", "", sent)  # then the bot crashed
        await executor.spot.market_order(BTC, 0.01, sent)
        executor.clock = lambda: T0 + timedelta(hours=9)  # back up nine hours later
        await executor.settle(Portfolio(1000.0))
        [fill] = ledger.fills()
        return fill

    assert run(fake, tmp_path, action).time == T0


def test_cash_noise_counts_in_the_quote_asset(tmp_path: Path) -> None:
    rules = {"ETHBTC": {"tick": "0.00001", "step": "0.0001", "minQty": "0.0001"}}
    rules["ETHBTC"] |= {"minNotional": "0.0001", "base": "ETH", "quote": "BTC"}
    fake = FakeSpot(balances={"BTC": 0.0}, prices={"ETHBTC": 0.03}, rules=rules)

    async def go() -> list[Adjustment]:
        async with fake.client() as client:
            spot = BinanceSpot(client, fake.api_key, fake.secret, base_url=TESTNET_URL)
            await spot.load_rules(["ETHBTC"])
            with Ledger(tmp_path / "ledger.sqlite") as ledger:
                return await reconcile(
                    spot, Portfolio(0.05), ["ETHBTC"], ledger, Collect(), T0, tolerance=0.002
                )

    [adjustment] = asyncio.run(go())  # 0.05 BTC is no noise; a dollar was the floor before
    assert adjustment.cash == pytest.approx(-0.05)


def test_an_order_minimum_in_the_wrong_unit_is_refused(tmp_path: Path) -> None:
    config = LiveConfig(
        mode="testnet",
        initial_cash=0.05,
        strategies=[
            StrategyConfig(
                name="donchian_trend",
                symbols=["ETHBTC"],
                timeframe=Timeframe.H4,
                allocation=1.0,
                params={"entry": 5, "exit": 3},
            )
        ],
        ledger=tmp_path / "testnet.sqlite",
    )
    btc = SymbolRules(
        "ETH", "BTC", Decimal("0.00001"), Decimal("0.0001"), Decimal("0.0001"), Decimal("0.0001")
    )
    usdt = SymbolRules(
        "BTC", "USDT", Decimal("0.01"), Decimal("0.00001"), Decimal("0.00001"), Decimal("5")
    )
    assert "rebalance.min_notional 10 counts in BTC" in quote_problem(config, {"ETHBTC": btc})
    assert quote_problem(config, {BTC: usdt}) == ""
    small = config.model_copy(
        update={"rebalance": config.rebalance.model_copy(update={"min_notional": 0.0002})}
    )
    assert quote_problem(small, {"ETHBTC": btc}) == ""


def test_a_stop_cut_short_at_shutdown_is_placed_before_the_end(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.01}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> list[str]:
        portfolio = Portfolio(0.0)
        portfolio.apply(Fill(T0, BTC, 0.01, 50000.0, 0.0))
        await executor.after_event(portfolio, MARKS)
        await executor._cancel_own_stops(BTC)  # the refit was cancelled here
        return await final_protect(executor, portfolio, MARKS)

    assert run(fake, tmp_path, action) == []
    assert [o["status"] for o in fake.orders] == ["CANCELED", "NEW"]


def test_a_stop_still_missing_at_the_end_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.01}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> list[str]:
        portfolio = Portfolio(0.0)
        portfolio.apply(Fill(T0, BTC, 0.01, 50000.0, 0.0))
        await executor.after_event(portfolio, MARKS)
        await executor._cancel_own_stops(BTC)

        async def slow(*args: object) -> Order:
            await asyncio.sleep(10)
            raise AssertionError("not reached")

        monkeypatch.setattr(executor.spot, "stop_loss_order", slow)
        monkeypatch.setattr("tbot.live.runner.FINAL_PROTECT_SECONDS", 0.05)
        return await final_protect(executor, portfolio, MARKS)

    assert run(fake, tmp_path, action) == [BTC]


def test_a_buy_ends_rather_than_cancel_the_owners_resting_order(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0, "BTC": 0.01}, prices={BTC: 50000.0})
    fake.own_resting = True  # the owner's order sits where the bot's would trade

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> list[str]:
        portfolio = Portfolio(1000.0)
        assert await executor.execute({BTC: 0.01}, T0, portfolio, MARKS) == []
        return notifier.messages

    messages = run(fake, tmp_path, action)
    assert fake.own_expired == 0  # the owner's order stays
    (order,) = [o for o in fake.orders if o["type"] == "MARKET"]
    assert order["selfTradePreventionMode"] == "EXPIRE_TAKER"
    assert "(EXPIRED_IN_MATCH: it met a resting order of this account (self-trade" in messages[0]


def test_a_sale_keeps_binances_self_trade_default_so_an_exit_goes_out(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.01}, prices={BTC: 50000.0})
    fake.own_resting = True

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> None:
        portfolio = Portfolio(0.0)
        portfolio.apply(Fill(T0, BTC, 0.01, 50000.0, 0.0))
        assert len(await executor.execute({BTC: -0.01}, T0, portfolio, MARKS)) == 1

    run(fake, tmp_path, action)
    assert [o["selfTradePreventionMode"] for o in fake.orders] == ["EXPIRE_MAKER"]
    assert fake.own_expired == 1


def test_a_pair_without_self_trade_modes_gets_no_mode() -> None:
    info = symbol_info(BTC, fake_rules()[BTC])
    info.pop("allowedSelfTradePreventionModes")
    assert parse_rules(info).self_trade_modes == frozenset()
    assert "EXPIRE_TAKER" in parse_rules(symbol_info(BTC, fake_rules()[BTC])).self_trade_modes


def test_an_order_found_still_working_is_left_in_doubt_not_booked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})
    fake.lose_next_order_response = True
    working = [True]
    handle = fake.handle

    def still_working(request: httpx.Request) -> httpx.Response:
        response = handle(request)
        lookup = request.method == "GET" and request.url.path == "/api/v3/order"
        if lookup and working[0] and response.status_code == 200:
            data = {**response.json(), "status": "NEW", "executedQty": "0.00000000"}
            return httpx.Response(200, json={**data, "cummulativeQuoteQty": "0.00000000"})
        return response

    monkeypatch.setattr(fake, "handle", still_working)

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> Portfolio:
        portfolio = Portfolio(1000.0)
        assert await executor.execute({BTC: 0.01}, T0, portfolio, MARKS) == []
        assert statuses(ledger) == ["pending", "unknown"]
        (note,) = ledger.conn.execute("SELECT note FROM orders WHERE status = 'unknown'").fetchone()
        assert "still working (NEW)" in note
        working[0] = False  # it executed in the end
        assert await executor.settle(portfolio) == 1
        return portfolio

    portfolio = run(fake, tmp_path, action)
    assert portfolio.position(BTC) == pytest.approx(0.00999)


def test_no_buy_goes_out_while_another_buy_is_in_doubt(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0, "BTC": 0.01}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> list[str]:
        ledger.add_order(T0, "ETHUSDT", 0.1, "unknown", "lost answer", "tbETHB")
        portfolio = Portfolio(1000.0)
        portfolio.apply(Fill(T0, BTC, 0.01, 50000.0, 0.0))
        assert await executor.execute({BTC: 0.005}, T0, portfolio, MARKS) == []
        assert statuses(ledger)[-1] == "skipped"
        sold = await executor.execute({BTC: -0.005}, T0, portfolio, MARKS)
        assert len(sold) == 1  # a sale still goes out
        return notifier.messages

    messages = run(fake, tmp_path, action)
    assert messages[0] == (
        f"[live] {BTC}: a buy whose result is not known yet may have spent the book's cash; "
        "no buy this bar"
    )


def test_a_kept_stop_flags_the_coins_bought_since(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0, "BTC": 0.01}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> list[str]:
        portfolio = Portfolio(1000.0)
        portfolio.apply(Fill(T0, BTC, 0.01, 50000.0, 0.0))
        await executor.after_event(portfolio, MARKS)  # a stop for 0.01 at 40,000
        await executor.execute({BTC: 0.005}, T0, portfolio, MARKS)
        fake.prices[BTC] = 47000.0  # down from a close at 60,000 since
        await executor.after_event(portfolio, {BTC: 60000.0})  # the old stop stays
        assert executor.partial[BTC] == pytest.approx(0.004995)
        return notifier.messages

    messages = run(fake, tmp_path, action)
    assert messages[-1] == (
        f"[live] {BTC}: the stop covers 0.010000 of 0.014995; the price fell through the new "
        "stop level, so the stop in place stays; the rest has no exchange stop"
    )
    assert stops(fake) == [("40000.00", "NEW")]


def test_an_answer_that_is_no_json_is_an_answer_in_doubt(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> None:
        fake.fail_next = [httpx.Response(200, text="<html>proxy</html>")]
        with pytest.raises(BinanceError) as caught:
            await executor.spot.balances()
        assert caught.value.uncertain
        assert "not a Binance answer" in str(caught.value)
        fake.error_after_next_order = httpx.Response(200, text="<html>proxy</html>")
        fills = await executor.execute({BTC: 0.01}, T0, Portfolio(1000.0), MARKS)
        assert len(fills) == 1  # looked up and booked, not taken for a refusal
        assert statuses(ledger) == ["pending", "filled"]

    run(fake, tmp_path, action)
