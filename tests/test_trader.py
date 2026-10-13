import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from factories import Scripted, price_bars
from tbot.core.config import StrategyConfig
from tbot.core.timeframe import Timeframe
from tbot.execution.rebalance import RebalanceRules
from tbot.execution.sim_broker import CostModel, SimulatedBroker
from tbot.live.config import PaperConfig
from tbot.live.executor import PaperExecutor
from tbot.live.history import BarHistory
from tbot.live.ledger import Ledger
from tbot.live.runner import (
    SessionTrader,
    StartRefused,
    check_budget,
    load_guard,
    restore_portfolio,
    resume,
    stop_path,
    watch_stop_request,
)
from tbot.live.session import TradingSession
from tbot.portfolio.allocation import StrategySlot
from tbot.portfolio.portfolio import Portfolio
from tbot.risk.guard import GuardConfig, RiskGuard

H1 = Timeframe.H1
T0 = datetime(2024, 1, 1, tzinfo=UTC)
BTC = ("BTC", H1)


def at(hours: int) -> datetime:
    return T0 + timedelta(hours=hours)


class Prices:
    def __init__(self) -> None:
        self.current = 100.0
        self.fail = False

    async def price(self, symbol: str, side: int) -> float:
        if self.fail:
            raise RuntimeError("quote down")
        return self.current


class Collect:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


def config_for(tmp_path: Path, guard: GuardConfig) -> PaperConfig:
    return PaperConfig(
        initial_cash=1000.0,
        costs=CostModel(fee_rate=0.001, slippage_bps=0),
        rebalance=RebalanceRules(min_notional=0, rebalance_threshold=0),
        strategies=[StrategyConfig(name="scripted", symbols=["BTC"], timeframe=H1, allocation=1)],
        ledger=tmp_path / "paper.sqlite",
        guard=guard,
    )


class Harness:
    def __init__(
        self, tmp_path: Path, guard: GuardConfig, script: dict[datetime, dict[str, float]]
    ) -> None:
        self.config = config_for(tmp_path, guard)
        self.prices = Prices()
        self.notifier = Collect()
        self.ledger = Ledger(self.config.ledger)
        session = TradingSession(
            self.config,
            [StrategySlot(Scripted(["BTC"], {"script": script}), H1, 1.0)],
            {BTC: BarHistory(H1)},
            Portfolio(self.config.initial_cash),
        )
        executor = PaperExecutor(
            SimulatedBroker(self.config.costs),
            self.prices,
            self.ledger,
            self.notifier,
            lambda: at(9),
        )
        self.trader = SessionTrader(
            session, self.ledger, executor, self.notifier, RiskGuard(guard), clock=lambda: at(9)
        )

    def bar(self, index: int, price: float) -> list[float]:
        """Deliver the bar with this index at this price; return fill quantities."""
        self.prices.current = price
        bars = price_bars(at(index), H1, [price], [price])
        fills = asyncio.run(self.trader.handle({BTC: bars}))
        return [f.quantity for f in fills]

    @property
    def equity(self) -> float:
        session = self.trader.session
        return session.portfolio.equity(session.marks)


def test_drawdown_halts_flattens_and_needs_resume(tmp_path: Path) -> None:
    guard = GuardConfig(daily_loss_limit=0, max_drawdown=0.1, stale_seconds=0)
    h = Harness(tmp_path, guard, {at(1): {"BTC": 1.0}})

    assert h.bar(0, 100.0) == [pytest.approx(1000 / 100.1)]  # all cash, fee included
    assert h.equity == pytest.approx(999.0, abs=0.01)
    assert h.bar(1, 85.0) == [pytest.approx(-1000 / 100.1)]  # -15%: kill switch flattens
    assert h.trader.session.portfolio.positions == {}
    assert load_guard(h.config, h.ledger).state.halted
    assert any("KILL SWITCH" in m for m in h.notifier.messages)
    assert h.ledger.recent_events(1)[0].message.startswith("kill switch: drawdown -15.1%")

    assert h.bar(2, 100.0) == []  # still halted: the strategy wants to be long, nothing trades
    assert resume(h.config).startswith("resumed")  # operator action, own connection
    assert h.bar(3, 100.0) == [pytest.approx(8.4745, rel=1e-3)]  # picked up, no restart
    assert resume(h.config) == "not halted"
    h.ledger.close()


def test_daily_loss_blocks_entries_but_allows_exits(tmp_path: Path) -> None:
    guard = GuardConfig(daily_loss_limit=0.03, max_drawdown=0, stale_seconds=0)
    script = {at(1): {"BTC": 1.0}, at(2): {"BTC": 0.0}, at(3): {"BTC": 1.0}}
    h = Harness(tmp_path, guard, script)

    assert h.bar(0, 100.0) == [pytest.approx(1000 / 100.1)]
    assert h.bar(1, 96.0) == [pytest.approx(-1000 / 100.1)]  # exit allowed under reduce-only
    assert h.bar(2, 96.0) == []  # re-entry blocked: still 4% below the day's open
    assert any("entries blocked" in m for m in h.notifier.messages)
    assert h.ledger.recent_events(1)[0].message.startswith("reduce only")
    h.ledger.close()


def test_stale_bar_is_blocked(tmp_path: Path) -> None:
    guard = GuardConfig(daily_loss_limit=0, max_drawdown=0, stale_seconds=60)
    h = Harness(tmp_path, guard, {at(1): {"BTC": 1.0}})
    assert h.bar(0, 100.0) == []  # bar closed at 1h, clock says 9h
    assert h.ledger.recent_events(1)[0].message.startswith("blocked: bar is")
    assert any("no trading this bar" in m for m in h.notifier.messages)
    h.ledger.close()


def test_halt_is_announced_once_while_dust_remains(tmp_path: Path) -> None:
    guard = GuardConfig(daily_loss_limit=0, max_drawdown=0.1, stale_seconds=0)
    h = Harness(tmp_path, guard, {at(1): {"BTC": 1.0}})
    assert len(h.bar(0, 100.0)) == 1
    h.prices.fail = True  # flattening cannot fill, so the position stays behind
    assert h.bar(1, 85.0) == []
    assert h.bar(2, 85.0) == []
    assert sum("KILL SWITCH" in m for m in h.notifier.messages) == 1
    h.prices.fail = False
    assert h.bar(3, 85.0) == [pytest.approx(-1000 / 100.1)]  # flattened now: announced again
    assert sum("KILL SWITCH" in m for m in h.notifier.messages) == 2
    h.ledger.close()


def test_blocked_bars_are_announced_once(tmp_path: Path) -> None:
    guard = GuardConfig(daily_loss_limit=0, max_drawdown=0, stale_seconds=60)
    h = Harness(tmp_path, guard, {})
    assert h.bar(0, 100.0) == []  # closed at 1h, the clock says 9h: stale
    assert h.bar(1, 100.0) == []
    assert sum("no trading this bar" in m for m in h.notifier.messages) == 1
    assert h.bar(8, 100.0) == []  # closes at 9h: fresh again
    assert h.notifier.messages[-1].endswith("trading again")
    h.ledger.close()


def test_failing_reconciliation_pauses_orders(tmp_path: Path) -> None:
    guard = GuardConfig(daily_loss_limit=0, max_drawdown=0, stale_seconds=0)
    h = Harness(tmp_path, guard, {at(1): {"BTC": 1.0}})
    h.trader.health.blocked = "reconciliation failing (down)"
    assert h.bar(0, 100.0) == []
    assert "reconciliation failing" in h.notifier.messages[0]
    h.trader.health.blocked = ""
    assert len(h.bar(1, 100.0)) == 1
    assert h.ledger.get_meta("targets") is not None  # strategy targets saved every event
    h.ledger.close()


def budget_trader(
    config: PaperConfig, ledger: Ledger, script: dict[datetime, dict[str, float]], prices: Prices
) -> SessionTrader:
    session = TradingSession(
        config,
        [StrategySlot(Scripted(["BTC"], {"script": script}), H1, 1.0)],
        {BTC: BarHistory(H1)},
        restore_portfolio(config, ledger),
    )
    executor = PaperExecutor(
        SimulatedBroker(config.costs), prices, ledger, Collect(), lambda: at(9)
    )
    guard = load_guard(config, ledger)
    return SessionTrader(session, ledger, executor, Collect(), guard, clock=lambda: at(9))


@pytest.mark.parametrize("budgets", [(400.0, 100.0), (3000.0, 6000.0), (1500.0, 2000.0)])
def test_two_budget_changes_before_a_bar_are_no_loss(
    tmp_path: Path, budgets: tuple[float, float]
) -> None:
    guard = GuardConfig(daily_loss_limit=0.03, max_drawdown=0.45, stale_seconds=0)
    config = config_for(tmp_path, guard)
    with Ledger(config.ledger) as ledger:
        check_budget(config, ledger, load_guard(config, ledger), at(0))
        trader = budget_trader(config, ledger, {}, Prices())
        asyncio.run(trader.handle({BTC: price_bars(at(0), H1, [100.0], [100.0])}))
        for hour, budget in enumerate(budgets, start=1):  # two restarts, no bar between
            changed = config.model_copy(update={"initial_cash": budget})
            check_budget(changed, ledger, load_guard(changed, ledger), at(hour))
        trader = budget_trader(changed, ledger, {}, Prices())
        asyncio.run(trader.handle({BTC: price_bars(at(1), H1, [100.0], [100.0])}))
        state = load_guard(changed, ledger).state
    assert not state.halted
    assert state.peak_equity == pytest.approx(budgets[-1])
    assert state.day_open_equity == pytest.approx(budgets[-1])


def test_a_budget_cut_beyond_what_the_book_holds_is_refused(tmp_path: Path) -> None:
    config = config_for(tmp_path, GuardConfig(stale_seconds=0))
    prices = Prices()
    with Ledger(config.ledger) as ledger:
        check_budget(config, ledger, load_guard(config, ledger), at(0))
        trader = budget_trader(config, ledger, {at(1): {"BTC": 1.0}}, prices)
        asyncio.run(trader.handle({BTC: price_bars(at(0), H1, [100.0], [100.0])}))  # buys
        prices.current = 60.0
        asyncio.run(trader.handle({BTC: price_bars(at(1), H1, [60.0], [60.0])}))
        worth = ledger.latest_equity()
        assert worth is not None
        smaller = config.model_copy(update={"initial_cash": 300.0})
        lowest = f"lowest it can be now is {1000 - worth.equity:,.2f}"
        with pytest.raises(StartRefused, match=lowest):
            check_budget(smaller, ledger, load_guard(smaller, ledger), at(2))
        assert ledger.adjustments() == []  # nothing booked


def test_a_stop_request_still_open_elsewhere_is_taken_once_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tbot.live.runner.STOP_POLL_SECONDS", 0.01)
    path = stop_path(tmp_path / "paper.sqlite")

    async def go() -> bool:
        stop = asyncio.Event()
        with path.open("w", encoding="utf-8") as handle:  # Windows cannot delete it yet
            handle.write("stop\n")
            handle.flush()
            task = asyncio.create_task(watch_stop_request(path, stop))
            await asyncio.sleep(0.05)
        await asyncio.wait_for(task, 2)
        return stop.is_set()

    assert asyncio.run(go())
    assert not path.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows cannot delete an open file")
def test_a_stop_request_open_elsewhere_still_stops_a_start(tmp_path: Path) -> None:
    from tbot.live.runner import take_stop_request

    ledger = tmp_path / "paper.sqlite"
    with stop_path(ledger).open("w", encoding="utf-8") as handle:
        handle.write("stop\n")
        handle.flush()
        assert take_stop_request(ledger)  # obeyed, though it cannot be removed yet
