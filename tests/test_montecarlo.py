import math

import numpy as np
import pytest

from tbot.research.montecarlo import simulate


def test_one_block_of_every_day_reproduces_the_curve() -> None:
    # Up 10%, down 20% while a trade is open, up 50%: the dip is in the daily path.
    returns = np.array([0.1, -0.2, 0.5])
    summary = simulate(returns, runs=50, seed=3, block_days=3, drawdown_limit=0.15)
    assert summary.days == 3
    assert summary.drawdown_p5 == pytest.approx(-0.2)
    assert summary.drawdown_p95 == pytest.approx(-0.2)
    assert summary.return_p50 == pytest.approx(1.1 * 0.8 * 1.5 - 1)
    assert summary.prob_drawdown_beyond == 1.0


def test_blocks_keep_streaks_together() -> None:
    # Three bad days in a row only stay together when blocks are long enough.
    returns = np.array([0.02] * 30 + [-0.1, -0.1, -0.1] + [0.02] * 30)
    short = simulate(returns, runs=2000, seed=1, block_days=1)
    long = simulate(returns, runs=2000, seed=1, block_days=20)
    assert long.drawdown_p50 < short.drawdown_p50
    assert short.return_p5 < short.return_p95  # resampling spreads the outcome


def test_empty_series() -> None:
    empty = simulate(np.zeros(0), runs=100)
    assert empty.days == 0
    assert math.isnan(empty.drawdown_p50)


def test_bars_within_a_day_count() -> None:
    # A 4h dip that recovers by the close: daily closes never see it, the guard does.
    bars = np.array([0.0, -0.3, 0.0, 0.0, 0.0, 0.3 / 0.7] * 10)
    summary = simulate(bars, runs=200, seed=1, block_days=2, kill_switch=0.25, per_day=6)
    assert summary.days == 10
    assert summary.block_days == 2
    assert summary.prob_kill == 1.0
