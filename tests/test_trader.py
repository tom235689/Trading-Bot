import asyncio
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
from tbot.live.guard import GuardConfig, RiskGuard
from tbot.live.history import BarHistory
from tbot.live.ledger import Ledger
from tbot.live.runner import SessionTrader, load_guard, resume
from tbot.live.session import TradingSession
from tbot.portfolio.allocation import StrategySlot
from tbot.portfolio.portfolio import Portfolio

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
