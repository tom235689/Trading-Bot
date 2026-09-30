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


def statuses(ledger: Ledger) -> list[str]:
    rows = ledger.conn.execute("SELECT status FROM orders ORDER BY id").fetchall()
    return [row[0] for row in rows]


def test_order_id_is_deterministic() -> None:
    assert order_id(T0, BTC, 1.0) == "tb1704067200000BTCUSDTB"
    assert order_id(T0, BTC, -1.0) == "tb1704067200000BTCUSDTS"
    assert len(order_id(T0, "A" * 40, 1.0)) == 36


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

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> None:
        portfolio = Portfolio(1000.0)
        fills = await executor.execute({BTC: 0.01}, T0, portfolio, MARKS)
        assert len(fills) == 1
        assert len(ledger.fills()) == 1
        assert statuses(ledger) == ["pending", "filled"]
        # The same event again (crash and restart): recovered, not re-sent.
        again = await executor.execute({BTC: 0.01}, T0, Portfolio(1000.0), MARKS)
        assert len(again) == 1

    run(fake, tmp_path, action)
    assert fake.order_count("MARKET") == 1


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
        return await executor.cancel_stops(BTC)

    assert run(fake, tmp_path, action) == 0
