import math
from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from tbot.backtest.engine import BacktestResult
from tbot.research.montecarlo import simulate, trade_returns_on_equity

T0 = datetime(2024, 1, 1, tzinfo=UTC)


def test_trade_returns_use_equity_at_entry() -> None:
    hours = [T0 + timedelta(hours=h) for h in (1, 2, 3)]
    result = BacktestResult(
        initial_cash=500.0,
        equity=pl.DataFrame({"time": hours, "equity": [1000.0, 1200.0, 1100.0]}),
        fills=pl.DataFrame(),
        trades=pl.DataFrame(
            {
                "entry_time": [T0, hours[0], hours[1] + timedelta(minutes=30)],
                "pnl": [50.0, 100.0, -60.0],
            }
        ),
        positions={},
    )
    returns = trade_returns_on_equity(result)
    assert returns.tolist() == pytest.approx([0.1, 0.1, -0.05])  # before any record: initial cash


def test_simulate_enumerates_orderings() -> None:
    # Two losses and a gain: 4 of 6 orderings put the losses back to back (-28%), 2 do not (-20%).
    summary = simulate(np.array([-0.2, -0.1, 0.5]), runs=3000, seed=7, drawdown_limit=0.25)
    assert summary.trades == 3
    assert summary.return_p5 == pytest.approx(0.08)
    assert summary.return_p95 == pytest.approx(0.08)
    assert summary.drawdown_p5 == pytest.approx(-0.28)
    assert summary.drawdown_p95 == pytest.approx(-0.2)
    assert summary.prob_drawdown_beyond == pytest.approx(2 / 3, abs=0.05)


def test_simulate_bootstrap_and_empty() -> None:
    boot = simulate(np.array([-0.2, 0.5]), runs=200, seed=1, replace=True)
    assert boot.return_p5 < boot.return_p95  # resampling varies the outcome
    empty = simulate(np.zeros(0), runs=100)
    assert empty.trades == 0
    assert math.isnan(empty.drawdown_p50)
