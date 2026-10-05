import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from binance_spot_fake import FakeSpot
from tbot.exchange.binance import NOTHING_TO_CANCEL, TESTNET_URL, BinanceError, BinanceSpot, Order
from tbot.live.executor import LiveExecutor, order_id
from tbot.live.ledger import Ledger
from tbot.portfolio.portfolio import Portfolio

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
    ids = [o["clientOrderId"] for o in fake.orders]
    assert ids == ["tb1704067200000BTCUSDTB", "tb1704067200000BTCUSDTBr2"]
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
