"""Buys pay at most buy_limit_pct over the close they were decided on; sales always go out."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from binance_spot_fake import FakeSpot
from tbot.core.config import StrategyConfig
from tbot.core.timeframe import Timeframe
from tbot.exchange.binance import TESTNET_URL, BinanceSpot
from tbot.execution.sim_broker import CostModel, SimulatedBroker
from tbot.live.config import PaperConfig
from tbot.live.executor import LIMIT_ROOM, LiveExecutor, PaperExecutor, limit_problem
from tbot.live.ledger import Ledger
from tbot.portfolio.portfolio import Portfolio

T0 = datetime(2024, 1, 1, tzinfo=UTC)
BTC = "BTCUSDT"
H4 = Timeframe.H4
CLOSE = 50000.0
ASK = CLOSE * 1.0001  # the fake's spread


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
    *,
    limit: float = 0.02,
    market_marks: bool = True,
) -> T:
    async def go() -> T:
        async with fake.client() as client:
            spot = BinanceSpot(
                client, fake.api_key, fake.secret, base_url=TESTNET_URL, clock=lambda: T0
            )
            await spot.load_rules([BTC])
            ledger, notifier = Ledger(tmp_path / "ledger.sqlite"), Collect()
            executor = LiveExecutor(
                spot,
                ledger,
                notifier,
                lambda: T0,
                fee_rate=0.001,
                protective_stop_pct=0.0,
                buy_limit_pct=limit,
                market_marks=market_marks,
            )
            try:
                return await action(executor, ledger, notifier)
            finally:
                ledger.close()

    return asyncio.run(go())


def orders(ledger: Ledger) -> list[tuple[str, str]]:
    rows = ledger.conn.execute("SELECT status, note FROM orders ORDER BY id").fetchall()
    return [(status, note) for status, note in rows]


def sent(fake: FakeSpot) -> list[dict[str, str]]:
    return [o for o in fake.orders if o["type"] in ("MARKET", "LIMIT")]


def test_a_buy_goes_out_as_a_limit_order_just_above_the_ask(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: CLOSE})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> None:
        fills = await executor.execute({BTC: 0.01}, T0, Portfolio(1000.0), {BTC: CLOSE})
        assert [round(f.price, 2) for f in fills] == [round(ASK, 2)]  # at the ask, as before
        assert [status for status, _ in orders(ledger)] == ["pending", "filled"]

    run(fake, tmp_path, action)
    (order,) = sent(fake)
    assert (order["type"], order["timeInForce"]) == ("LIMIT", "IOC")
    assert order["selfTradePreventionMode"] == "EXPIRE_TAKER"  # the owner's order stays
    assert float(order["price"]) == pytest.approx(ASK * (1 + LIMIT_ROOM), abs=0.01)
    assert float(order["price"]) <= ASK * (1 + LIMIT_ROOM)  # rounded down to the tick


def test_a_buy_whose_ask_ran_past_the_limit_is_not_sent(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: CLOSE * 1.03})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> None:
        fills = await executor.execute({BTC: 0.01}, T0, Portfolio(1000.0), {BTC: CLOSE})
        assert fills == []
        ((status, detail),) = orders(ledger)
        assert status == "skipped"
        assert "3.0% above the close 50,000.00 it was decided on" in detail
        assert notifier.messages == [
            f"[live] no {BTC} buy this bar: {detail}; the next close decides again"
        ]

    run(fake, tmp_path, action)
    assert sent(fake) == []


def test_the_limit_is_the_close_plus_the_allowance_when_that_is_lower(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: CLOSE * 1.019})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> None:
        await executor.execute({BTC: 0.01}, T0, Portfolio(1000.0), {BTC: CLOSE})

    run(fake, tmp_path, action)
    (order,) = sent(fake)
    assert float(order["price"]) == CLOSE * 1.02  # not ask times 1.005, which is higher
    assert order["status"] == "FILLED"


def test_a_buy_of_all_the_cash_fits_what_binance_holds_for_its_limit(tmp_path: Path) -> None:
    # Binance holds quantity times the limit, not the ask: sized on the ask, the order
    # would ask for more than the account has and be refused.
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: CLOSE})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> Portfolio:
        portfolio = Portfolio(1000.0)
        fills = await executor.execute({BTC: 1.0}, T0, portfolio, {BTC: CLOSE})
        assert len(fills) == 1
        return portfolio

    portfolio = run(fake, tmp_path, action)
    (order,) = sent(fake)
    assert float(order["origQty"]) * float(order["price"]) <= 1000.0
    assert portfolio.cash >= 0
    assert portfolio.cash < 10.0  # nearly all of it spent


def test_a_price_that_moves_past_the_limit_as_it_is_sent_buys_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: CLOSE})
    handle = fake.handle

    def jumpy(request: httpx.Request) -> httpx.Response:
        response = handle(request)
        if request.url.path == "/api/v3/ticker/bookTicker":
            fake.prices[BTC] *= 1.01  # after the quote, before the order
        return response

    monkeypatch.setattr(fake, "handle", jumpy)

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> None:
        portfolio = Portfolio(1000.0)
        assert await executor.execute({BTC: 0.01}, T0, portfolio, {BTC: CLOSE}) == []
        assert portfolio.cash == 1000.0
        assert [status for status, _ in orders(ledger)] == ["pending", "unfilled"]
        (message,) = notifier.messages
        assert message.startswith(f"[live] the {BTC} buy executed 0.000000 of 0.010000 within")
        assert message.endswith("the price moved past it; the rest waits for the next decision")

    run(fake, tmp_path, action)
    assert fake.balances["USDT"] == 1000.0


def test_part_of_a_buy_within_its_limit_is_booked(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: CLOSE}, market_fraction=0.5)

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> None:
        portfolio = Portfolio(1000.0)
        fills = await executor.execute({BTC: 0.01}, T0, portfolio, {BTC: CLOSE})
        assert fills[0].quantity == pytest.approx(0.005 * 0.999)  # half, less its commission
        assert "buy executed 0.005000 of 0.010000 within its limit" in notifier.messages[0]

    run(fake, tmp_path, action)


def test_sales_stay_market_orders(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.01}, prices={BTC: CLOSE * 0.5})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> None:
        portfolio = Portfolio(0.0)
        portfolio.positions[BTC] = 0.01
        fills = await executor.execute({BTC: -0.01}, T0, portfolio, {BTC: CLOSE})
        assert len(fills) == 1  # half the close: an exit still goes out

    run(fake, tmp_path, action)
    assert [o["type"] for o in sent(fake)] == ["MARKET"]


def test_no_limit_sends_market_buys(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: CLOSE * 1.5})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> None:
        await executor.execute({BTC: 0.01}, T0, Portfolio(1000.0), {BTC: CLOSE})

    run(fake, tmp_path, action, limit=0.0)
    assert [o["type"] for o in sent(fake)] == ["MARKET"]


def test_testnet_prices_its_limit_on_its_own_ask(tmp_path: Path) -> None:
    # Testnet trades at prices of its own, far from the market's closes.
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: CLOSE * 1.3})

    async def action(executor: LiveExecutor, ledger: Ledger, notifier: Collect) -> None:
        fills = await executor.execute({BTC: 0.001}, T0, Portfolio(1000.0), {BTC: CLOSE})
        assert len(fills) == 1

    run(fake, tmp_path, action, market_marks=False)
    (order,) = sent(fake)
    assert order["type"] == "LIMIT"
    assert float(order["price"]) <= CLOSE * 1.3 * 1.0001 * (1 + LIMIT_ROOM)


class Prices:
    def __init__(self, price: float) -> None:
        self.value = price

    async def price(self, symbol: str, side: int) -> float:
        return self.value


def test_paper_skips_a_buy_beyond_the_limit_and_fills_one_within(tmp_path: Path) -> None:
    async def go() -> None:
        prices, notifier = Prices(103.0), Collect()
        with Ledger(tmp_path / "paper.sqlite") as ledger:
            executor = PaperExecutor(
                SimulatedBroker(CostModel()),
                prices,
                ledger,
                notifier,
                lambda: T0,
                buy_limit_pct=0.02,
            )
            portfolio = Portfolio(1000.0)
            assert await executor.execute({"BTC": 1.0}, T0, portfolio, {"BTC": 100.0}) == []
            assert notifier.messages[0].startswith("[paper] no BTC buy this bar: the ask 103")
            prices.value = 101.0
            fills = await executor.execute({"BTC": 1.0}, T0, portfolio, {"BTC": 100.0})
            assert len(fills) == 1
            prices.value = 50.0
            sold = await executor.execute({"BTC": -1.0}, T0, portfolio, {"BTC": 100.0})
            assert len(sold) == 1  # sales are never held back

    asyncio.run(go())


def test_limit_problem_names_the_gap() -> None:
    assert limit_problem(102.0, 100.0, 0.02) == ""
    assert limit_problem(102.5, 100.0, 0.02).startswith("the ask 102.50 is 2.5% above the close")
    assert limit_problem(100.0, 0.0, 0.02) == "no close to price it on"


def test_a_buy_limit_meant_as_a_percent_is_refused(tmp_path: Path) -> None:
    strategies = [
        StrategyConfig(name="donchian_trend", symbols=["BTCUSDT"], timeframe=H4, allocation=1)
    ]
    assert PaperConfig(strategies=strategies).buy_limit_pct == 0.02
    with pytest.raises(ValidationError, match=r"at most 0.5: a fraction \(0.02 means 2%\)"):
        PaperConfig(strategies=strategies, buy_limit_pct=2)
    assert PaperConfig(strategies=strategies, buy_limit_pct=0).buy_limit_pct == 0
