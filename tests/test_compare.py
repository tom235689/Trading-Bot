import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from factories import price_bars
from tbot.cli import EXIT_CONFIG, main
from tbot.core.config import StrategyConfig
from tbot.core.models import Fill
from tbot.core.timeframe import Timeframe
from tbot.data.store import BarStore
from tbot.execution.rebalance import RebalanceRules
from tbot.execution.sim_broker import CostModel, SimulatedBroker
from tbot.live.compare import compare_session, comparison_text, match_fills
from tbot.live.config import PaperConfig
from tbot.live.executor import PaperExecutor
from tbot.live.ledger import Adjustment, Ledger
from tbot.live.runner import SessionTrader, build_session, restore_portfolio
from tbot.risk.guard import GuardConfig, RiskGuard

H4 = Timeframe.H4
T0 = datetime(2024, 1, 1, tzinfo=UTC)
KEY = ("BTCUSDT", H4)
SESSION_START = 200  # bars of history before the session starts
SESSION_BARS = 120


def closes() -> list[float]:
    """Up 2% for 15 bars, then down 2% for 15 bars, again and again."""
    values = [100.0]
    for i in range(1, 400):
        values.append(values[-1] * (1.02 if (i // 15) % 2 == 0 else 0.98))
    return values


class BookPrices:
    """The last close as the book price, made worse by spread_bps for the taker."""

    def __init__(self, spread_bps: float) -> None:
        self.close = 0.0
        self.spread = spread_bps / 1e4

    async def price(self, symbol: str, side: int) -> float:
        return self.close * (1 + side * self.spread)


class Quiet:
    async def send(self, text: str) -> bool:
        return True


def config(tmp_path: Path) -> PaperConfig:
    return PaperConfig(
        initial_cash=10_000.0,
        costs=CostModel(fee_rate=0.001, slippage_bps=5),
        rebalance=RebalanceRules(min_notional=10, rebalance_threshold=0.02),
        strategies=[
            StrategyConfig(
                name="donchian_trend",
                symbols=["BTCUSDT"],
                timeframe=H4,
                allocation=1.0,
                params={"entry": 10, "exit": 5},
            )
        ],
        ledger=tmp_path / "paper.sqlite",
        guard=GuardConfig(daily_loss_limit=0, max_drawdown=0, stale_seconds=0),
    )


def run_session(
    tmp_path: Path, *, spread_bps: float = 0.0, skip: range = range(0)
) -> tuple[PaperConfig, BarStore]:
    """Trade a paper session bar by bar, as the feed would hand bars to it."""
    values = closes()
    bars = price_bars(T0, H4, [values[0], *values[:-1]], values)
    store = BarStore(tmp_path / "data")
    store.write(*KEY, bars)
    cfg = config(tmp_path)
    prices = BookPrices(spread_bps)
    now = T0 + H4.delta * SESSION_START
    with Ledger(cfg.ledger) as ledger:
        session = build_session(cfg, store, restore_portfolio(cfg, ledger), now)
        executor = PaperExecutor(
            SimulatedBroker(cfg.costs), prices, ledger, Quiet(), lambda: now + timedelta(seconds=2)
        )
        trader = SessionTrader(
            session, ledger, executor, Quiet(), RiskGuard(cfg.guard), clock=lambda: now
        )
        for i in range(SESSION_START, SESSION_START + SESSION_BARS):
            now = T0 + H4.delta * (i + 1)  # the close of bar i
            if i - SESSION_START in skip:  # the bot was down
                continue
            prices.close = values[i]
            asyncio.run(trader.handle({KEY: bars.slice(i, 1)}))
    return cfg, store


def test_a_paper_session_tracks_its_backtest(tmp_path: Path) -> None:
    cfg, store = run_session(tmp_path)
    with Ledger(cfg.ledger) as ledger:
        comparison = compare_session(cfg, ledger, store)

    assert comparison.bar_events == SESSION_BARS
    assert comparison.missed == []
    assert len(comparison.matched) >= 6
    assert comparison.session_only == comparison.backtest_only == []
    for match in comparison.matched:  # same decisions, same sizes, same prices
        assert match.session.quantity == pytest.approx(match.backtest.quantity, rel=1e-6)
        assert match.extra_cost_bps == pytest.approx(0, abs=1e-6)
    assert comparison.max_gap < 0.005
    assert comparison.checks() == []
    assert comparison_text(comparison, "paper.sqlite").endswith("verdict: tracks the backtest")


def test_downtime_and_extra_costs_are_flagged(tmp_path: Path) -> None:
    cfg, store = run_session(tmp_path, spread_bps=20, skip=range(40, 50))
    with Ledger(cfg.ledger) as ledger:
        comparison = compare_session(cfg, ledger, store)

    assert len(comparison.missed) == 10
    assert comparison.mean_extra_cost_bps == pytest.approx(20, abs=0.5)
    text = comparison_text(comparison, "paper.sqlite")
    assert "10 bar events without a session snapshot" in text
    assert "fills cost 20.0 bps more than the backtest" in text
    assert text.endswith("verdict: does not track the backtest yet; see the checks")


def test_match_fills_pairs_by_symbol_side_and_time() -> None:
    def fill(hours: float, symbol: str, quantity: float) -> Fill:
        return Fill(T0 + timedelta(hours=hours), symbol, quantity, 100.0, 0.1)

    session = [fill(4.001, "BTC", 1.0), fill(8.001, "ETH", -1.0), fill(30, "BTC", 1.0)]
    backtest = [fill(4, "BTC", 1.1), fill(8, "ETH", 1.0), fill(8, "ETH", -0.9)]
    matched, session_only, backtest_only = match_fills(session, backtest, timedelta(hours=4))
    assert [(m.session.symbol, m.backtest.quantity) for m in matched] == [
        ("BTC", 1.1),
        ("ETH", -0.9),
    ]
    assert session_only == [session[2]]
    assert backtest_only == [backtest[1]]


def test_compare_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg, _ = run_session(tmp_path)
    path = tmp_path / "paper.yaml"
    path.write_text(cfg.model_dump_json(), encoding="utf-8")
    assert main(["compare", str(path), "--data-dir", str(tmp_path / "data")]) == 0
    assert "verdict: tracks the backtest" in capsys.readouterr().out
    fresh = cfg.model_copy(update={"ledger": tmp_path / "never_ran.sqlite"})
    path.write_text(fresh.model_dump_json(), encoding="utf-8")
    assert main(["compare", str(path)]) == EXIT_CONFIG
    assert "no ledger" in capsys.readouterr().err


def test_money_moved_in_by_reconciliation_is_taken_out(tmp_path: Path) -> None:
    cfg, store = run_session(tmp_path)
    with Ledger(cfg.ledger) as ledger:
        first = ledger.equity_points()[0].time
        # An account holding 500 more than initial_cash: the startup reconcile books it.
        ledger.add_adjustment(Adjustment(first - timedelta(minutes=1), "", 0.0, 500.0, "cash"))
        ledger.conn.execute("UPDATE equity SET equity = equity + 500")
        ledger.conn.commit()
        comparison = compare_session(cfg, ledger, store)
    assert comparison.flows == pytest.approx(500.0)
    assert comparison.max_gap < 0.005
    assert comparison.checks() == []
    assert "moved +500.00" in comparison_text(comparison, "paper.sqlite")


def test_a_protective_stop_fill_is_named_not_priced(tmp_path: Path) -> None:
    cfg, store = run_session(tmp_path)
    with Ledger(cfg.ledger) as ledger:
        position = sum(f.quantity for f in ledger.fills())
        when = ledger.equity_points()[5].time + timedelta(minutes=30)
        stop = Fill(when, "BTCUSDT", -abs(position) or -0.01, 1.0, 0.0)
        ledger.add_fill(stop, 1.0)
        ledger.add_order(when, "BTCUSDT", stop.quantity, "filled", "protective stop", "tbs1")
        comparison = compare_session(cfg, ledger, store)
    assert comparison.stop_fills == 1
    assert stop not in comparison.session_only
    assert all(m.session != stop for m in comparison.matched)
    assert "1 protective stop fills" in comparison_text(comparison, "paper.sqlite")
