"""Faults found by a randomized simulation of live sessions (crashes, lost answers,
delistings, budget changes), each reduced to its shortest sequence."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from binance_spot_fake import FakeSpot
from factories import Scripted, price_bars
from tbot.core.config import StrategyConfig
from tbot.core.models import Fill
from tbot.core.timeframe import Timeframe
from tbot.exchange.binance import TESTNET_URL, BinanceSpot, Order
from tbot.live.config import LiveConfig, Settings
from tbot.live.executor import LiveExecutor, order_id
from tbot.live.history import BarHistory
from tbot.live.ledger import EquityPoint, Ledger
from tbot.live.runner import (
    BUDGET_META,
    SessionTrader,
    StartRefused,
    check_budget,
    check_key_rights,
    load_checkpoint,
    reconcile_round,
    run_live,
)
from tbot.live.session import TradingSession
from tbot.portfolio.allocation import StrategySlot
from tbot.portfolio.portfolio import Portfolio
from tbot.risk.guard import GuardConfig, Mode, RiskGuard

T0 = datetime(2024, 1, 1, tzinfo=UTC)
BTC, ETH = "BTCUSDT", "ETHUSDT"
MARKS = {BTC: 50000.0, ETH: 3000.0}
H4 = Timeframe.H4


class Collect:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


Action = Callable[[LiveExecutor, Ledger, Collect, Clock], Awaitable[object]]


def run(fake: FakeSpot, tmp_path: Path, action: Action, stop_pct: float = 0.2) -> object:
    clock = Clock()

    async def go() -> object:
        async with fake.client() as client:
            spot = BinanceSpot(client, fake.api_key, fake.secret, base_url=TESTNET_URL, clock=clock)
            await spot.load_rules([BTC, ETH])
            ledger, notifier = Ledger(tmp_path / "ledger.sqlite"), Collect()
            executor = LiveExecutor(
                spot, ledger, notifier, clock, fee_rate=0.001, protective_stop_pct=stop_pct
            )
            try:
                return await action(executor, ledger, notifier, clock)
            finally:
                ledger.close()

    return asyncio.run(go())


@pytest.fixture(autouse=True)
def fast_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tbot.live.executor.LOOKUP_DELAYS", (0.0, 0.0, 0.0))


def live_config(tmp_path: Path, ownership: str = "budget", **update: object) -> LiveConfig:
    config = LiveConfig.model_validate(
        {
            "mode": "testnet",
            "initial_cash": 1000.0,
            "ownership": ownership,
            "strategies": [
                StrategyConfig(name="donchian_trend", symbols=[BTC], timeframe=H4, allocation=1.0)
            ],
            "ledger": tmp_path / "unused.sqlite",
        }
    )
    return config.model_copy(update=update)


def not_listed_yet(executor: LiveExecutor, monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """Binance does not list the order yet (-2013), as it may for a while after a lost
    answer; the returned function makes it known again."""
    lookup = executor.spot.get_order

    async def unknown(symbol: str, client_id: str, attempts: int | None = None) -> Order | None:
        return None

    monkeypatch.setattr(executor.spot, "get_order", unknown)
    return lambda: monkeypatch.setattr(executor.spot, "get_order", lookup)


def open_stops(fake: FakeSpot) -> int:
    return sum(1 for o in fake.orders if o["type"] == "STOP_LOSS" and o["status"] == "NEW")


def test_a_buy_in_doubt_is_not_reconciled_as_a_withdrawal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0})
    config = live_config(tmp_path)

    async def action(ex: LiveExecutor, ledger: Ledger, notifier: Collect, clock: Clock) -> object:
        portfolio, guard = Portfolio(1000.0), RiskGuard(GuardConfig())
        fake.lose_next_order_response = True  # it executes; the answer is lost
        listed = not_listed_yet(ex, monkeypatch)
        await ex.execute({BTC: 0.01}, T0, portfolio, MARKS)
        clock.now = T0 + timedelta(seconds=30)  # a reconciliation while it is in doubt
        await reconcile_round(ex, portfolio, MARKS, ledger, notifier, guard, config, clock)
        listed()
        clock.now = T0 + timedelta(seconds=330)
        await reconcile_round(ex, portfolio, MARKS, ledger, notifier, guard, config, clock)
        return portfolio

    portfolio = run(fake, tmp_path, action, stop_pct=0.0)
    assert isinstance(portfolio, Portfolio)
    assert portfolio.position(BTC) == pytest.approx(0.00999)
    assert portfolio.cash == pytest.approx(fake.balances["USDT"], abs=1.0)


def test_a_sell_in_doubt_after_a_restart_is_booked_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.02}, prices={BTC: 50000.0})
    config = live_config(tmp_path, ownership="account", initial_cash=0.0001)

    async def action(ex: LiveExecutor, ledger: Ledger, notifier: Collect, clock: Clock) -> object:
        portfolio, guard = Portfolio(1000.0), RiskGuard(GuardConfig())  # all in BTC
        portfolio.apply(Fill(T0, BTC, 0.02, 50000.0, 0.0))
        ledger.add_order(T0, BTC, -0.02, "pending", "", "tb-sent")  # the write-ahead row
        await ex.spot.market_order(BTC, -0.02, "tb-sent")  # executed; then the process died
        listed = not_listed_yet(ex, monkeypatch)
        clock.now = T0 + timedelta(seconds=45)  # the restart's reconciliation
        await reconcile_round(ex, portfolio, MARKS, ledger, notifier, guard, config, clock)
        listed()
        clock.now = T0 + timedelta(seconds=345)
        await reconcile_round(ex, portfolio, MARKS, ledger, notifier, guard, config, clock)
        assert portfolio.position(BTC) == pytest.approx(0.0)
        return [a.note for a in ledger.adjustments()]

    assert run(fake, tmp_path, action, stop_pct=0.0) == []  # no transfer in either direction


def test_a_budget_cut_after_a_stop_sold_while_down_scales_the_guard_from_the_real_book(
    tmp_path: Path,
) -> None:
    # The last bar valued the book at 1000 (all BTC, the peak). A stop then sold it at a
    # 20% loss while the bot was down; it restarts with initial_cash lowered by 500.
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.02}, prices={BTC: 50000.0})
    config = live_config(tmp_path, initial_cash=500.0)

    async def action(ex: LiveExecutor, ledger: Ledger, notifier: Collect, clock: Clock) -> object:
        fill = Fill(T0, BTC, 0.02, 50000.0, 0.0)
        ledger.add_fill(fill, 50000.0)
        ledger.add_equity(EquityPoint(T0, 1000.0, 0.0, 1.0))
        ledger.set_meta(BUDGET_META, "1000.0")
        portfolio = Portfolio(1000.0)
        portfolio.apply(fill)
        guard = RiskGuard(GuardConfig(daily_loss_limit=0, max_drawdown=0.35, stale_seconds=0))
        guard.check(T0, 1000.0, T0)
        await ex.after_event(portfolio, MARKS)  # stop at 40000
        fake.trigger_stop(BTC)  # executes at 40000: the book is now worth about 799
        clock.now = T0 + timedelta(hours=1)
        check_budget(config, ledger, guard, clock.now)  # the restart: 500 taken out
        portfolio.adjust("", 0.0, -500.0)  # the restored book has the cut
        await reconcile_round(ex, portfolio, MARKS, ledger, notifier, guard, config, clock)
        equity = portfolio.equity({BTC: 40000.0})  # about 299: 20% below a real peak of 374
        assert equity == pytest.approx(299.2, abs=0.5)
        later = T0 + timedelta(hours=4)
        return guard.check(later, equity, later).mode

    assert run(fake, tmp_path, action) != Mode.HALT


def test_a_stop_refit_on_outdated_rules_keeps_the_position_protected(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.01234}, prices={BTC: 50000.0})

    async def action(ex: LiveExecutor, ledger: Ledger, notifier: Collect, clock: Clock) -> object:
        portfolio = Portfolio(0.0)
        portfolio.apply(Fill(T0, BTC, 0.01234, 50000.0, 0.0))
        await ex.after_event(portfolio, MARKS)
        fake.rules[BTC] = {**fake.rules[BTC], "step": "0.001", "minQty": "0.001"}
        await ex.after_event(portfolio, MARKS)  # refused on the cached step, then retried
        return open_stops(fake)

    assert run(fake, tmp_path, action) == 1


def test_a_finer_lot_step_found_by_reconciliation_gets_the_remainder_a_stop(
    tmp_path: Path,
) -> None:
    rules = dict(FakeSpot(balances={}, prices={}).rules)
    rules[BTC] = {**rules[BTC], "step": "0.001", "minQty": "0.001"}
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.00086}, prices={BTC: 50000.0}, rules=rules)
    config = live_config(tmp_path)

    async def action(ex: LiveExecutor, ledger: Ledger, notifier: Collect, clock: Clock) -> object:
        portfolio = Portfolio(43.0)
        portfolio.apply(Fill(T0, BTC, 0.00086, 50000.0, 0.0))  # below one 0.001 step
        await ex.after_event(portfolio, MARKS)
        fake.rules[BTC] = {**fake.rules[BTC], "step": "0.0001", "minQty": "0.0001"}
        ex.spot.rules_time = float("-inf")  # the hourly reload falls in this round
        guard = RiskGuard(GuardConfig())
        await reconcile_round(ex, portfolio, MARKS, ledger, notifier, guard, config, clock)
        return open_stops(fake)

    assert run(fake, tmp_path, action) == 1


def test_a_book_less_than_a_step_above_the_account_is_not_called_locked(
    tmp_path: Path,
) -> None:
    rules = dict(FakeSpot(balances={}, prices={}).rules)
    rules[BTC] = {**rules[BTC], "step": "0.001", "minQty": "0.001"}
    fake = FakeSpot(balances={"USDT": 100.0, "BTC": 0.00099}, prices={BTC: 50000.0}, rules=rules)
    config = live_config(tmp_path, ownership="account", initial_cash=100.0)

    async def action(ex: LiveExecutor, ledger: Ledger, notifier: Collect, clock: Clock) -> object:
        portfolio = Portfolio(100.0)
        portfolio.positions[BTC] = 0.00183  # within one step of the 0.00099 held
        guard = RiskGuard(GuardConfig())
        await reconcile_round(ex, portfolio, MARKS, ledger, notifier, guard, config, clock)
        await ex.after_event(portfolio, MARKS)
        assert ex.unprotected == set()  # nothing on the exchange a stop can hold
        return notifier.messages

    messages = run(fake, tmp_path, action)
    assert isinstance(messages, list)
    assert not any("other orders lock" in m for m in messages)


def test_a_stop_that_sold_before_the_close_is_booked_before_the_guard_looks(
    tmp_path: Path,
) -> None:
    fake = FakeSpot(balances={"USDT": 0.0, "BTC": 0.02}, prices={BTC: 50000.0})
    config = live_config(tmp_path)
    close = T0 + H4.delta

    async def action(ex: LiveExecutor, ledger: Ledger, notifier: Collect, clock: Clock) -> object:
        portfolio = Portfolio(1000.0)
        portfolio.apply(Fill(T0, BTC, 0.02, 50000.0, 0.0))
        guard = RiskGuard(GuardConfig(daily_loss_limit=0, max_drawdown=0.45, stale_seconds=0))
        guard.check(T0, 1000.0, T0)
        await ex.after_event(portfolio, MARKS)  # stop at 40000
        fake.trigger_stop(BTC)  # a flash crash sells at 40000 a minute before the close
        strategy = Scripted([BTC], {"script": {close: {BTC: 1.0}}})
        session = TradingSession(
            config, [StrategySlot(strategy, H4, 1.0)], {(BTC, H4): BarHistory(H4)}, portfolio
        )
        clock.now = close + timedelta(seconds=2)
        trader = SessionTrader(session, ledger, ex, notifier, guard, clock, "testnet")
        await trader.handle({(BTC, H4): price_bars(T0, H4, [50000.0], [55000.0])})
        return guard.state.peak_equity

    peak = run(fake, tmp_path, action)
    assert isinstance(peak, float)
    assert peak <= 1000.0 + 1e-6  # the coins sold at 40000 are not valued at 55000


def test_a_restart_after_a_crash_mid_sell_puts_the_stop_back_at_once(tmp_path: Path) -> None:
    # The crash came after the stop was cancelled for a sell and its write-ahead row was
    # written, before the sell reached Binance; the bot comes back three hours later.
    fake = FakeSpot(balances={"USDT": 500.0, "BTC": 0.01}, prices={BTC: 50000.0})
    config = live_config(tmp_path)

    async def action(ex: LiveExecutor, ledger: Ledger, notifier: Collect, clock: Clock) -> object:
        ledger.add_order(T0, BTC, -0.01, "pending", "", order_id(T0, BTC, -0.01, tag="x"))
        clock.now = T0 + timedelta(hours=3)
        portfolio = Portfolio(500.0)
        portfolio.positions[BTC] = 0.01
        guard = RiskGuard(GuardConfig())
        await reconcile_round(ex, portfolio, MARKS, ledger, notifier, guard, config, clock)
        await ex.after_event(portfolio, MARKS)
        return open_stops(fake)

    assert run(fake, tmp_path, action) == 1


def test_a_pair_binance_stops_listing_with_a_stop_in_place_fails_no_round(
    tmp_path: Path,
) -> None:
    fake = FakeSpot(balances={"USDT": 1000.0, "ETH": 1.0}, prices={BTC: 50000.0, ETH: 3000.0})
    config = live_config(tmp_path).model_copy(
        update={
            "strategies": [
                StrategyConfig(
                    name="donchian_trend", symbols=[BTC, ETH], timeframe=H4, allocation=1.0
                )
            ]
        }
    )

    async def action(ex: LiveExecutor, ledger: Ledger, notifier: Collect, clock: Clock) -> object:
        portfolio = Portfolio(1000.0)
        portfolio.positions[ETH] = 1.0
        await ex.after_event(portfolio, MARKS)
        assert open_stops(fake) == 1  # the ETH stop is tracked in the ledger
        del fake.rules[ETH]
        fake.delisted.add(ETH)  # every call naming it fails from now on
        ex.spot.rules_time = float("-inf")
        guard = RiskGuard(GuardConfig())
        for _ in range(3):
            await reconcile_round(ex, portfolio, MARKS, ledger, notifier, guard, config, clock)
        return ex.spot.status[ETH]

    assert run(fake, tmp_path, action) == "UNLISTED"


def test_the_checkpoint_does_not_wait_for_a_stream_that_brings_nothing(tmp_path: Path) -> None:
    btc, eth = (BTC, H4), (ETH, H4)
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={BTC: 50000.0, ETH: 3000.0})
    config = live_config(tmp_path)

    async def action(ex: LiveExecutor, ledger: Ledger, notifier: Collect, clock: Clock) -> object:
        strategy = Scripted([BTC, ETH], {"script": {}})
        histories = {btc: BarHistory(H4), eth: BarHistory(H4)}
        session = TradingSession(config, [StrategySlot(strategy, H4, 1.0)], histories, Portfolio(0))
        clock.now = T0 + H4.delta
        trader = SessionTrader(session, ledger, ex, notifier, RiskGuard(GuardConfig()), clock)
        await trader.handle({btc: price_bars(T0, H4, [50000.0], [50000.0])})
        waits = load_checkpoint(config, ledger)  # ETH may still come for this close
        trader.quiet = lambda: {eth}  # stale, or paused by Binance
        later = T0 + H4.delta
        clock.now = later + H4.delta
        await trader.handle({btc: price_bars(later, H4, [50000.0], [50000.0])})
        return waits, load_checkpoint(config, ledger)

    result = run(fake, tmp_path, action, stop_pct=0.0)
    assert isinstance(result, tuple)
    waits, saved = result
    assert waits is None
    assert saved is not None


def test_real_money_needs_alerts_and_a_key_that_cannot_withdraw(tmp_path: Path) -> None:
    config = live_config(tmp_path).model_copy(update={"mode": "live"})
    keys = Settings(  # type: ignore[call-arg]
        _env_file=None, binance_api_key="key", binance_api_secret="secret"
    )
    with pytest.raises(StartRefused, match="set up Telegram with `tbot notify`"):
        asyncio.run(run_live(config, keys, tmp_path, confirmed=True))
    assert not config.ledger.exists()  # refused before a ledger was made

    fake = FakeSpot(balances={}, prices={})
    fake.restrictions["enableWithdrawals"] = True

    async def rights() -> None:
        async with fake.client() as client:
            await check_key_rights(BinanceSpot(client, fake.api_key, fake.secret))

    with pytest.raises(StartRefused, match="the API key can withdraw"):
        asyncio.run(rights())
    fake.restrictions["enableWithdrawals"] = False
    asyncio.run(rights())  # trading on, withdrawals off: it starts
