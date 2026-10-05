"""Monte Carlo on the equity curve: how bad could the drawdown have been?

Daily returns are resampled in blocks (a moving block bootstrap). Losses while a trade is
still open count, and so do streaks of bad days, which shuffling closed trades would hide.
"""

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt


@dataclass(frozen=True)
class MonteCarloSummary:
    runs: int
    days: int
    block_days: int
    drawdown_p5: float  # 5th percentile of max drawdown (worst tail)
    drawdown_p50: float
    drawdown_p95: float
    return_p5: float
    return_p50: float
    return_p95: float
    prob_drawdown_beyond: float  # share of runs with a drawdown worse than the limit
    drawdown_limit: float


def simulate(
    returns: npt.NDArray[np.float64],
    *,
    runs: int = 2000,
    seed: int = 1,
    block_days: int = 20,
    drawdown_limit: float = 0.25,
) -> MonteCarloSummary:
    """Compound paths of daily returns drawn as random blocks of consecutive days."""
    n = len(returns)
    block = max(1, min(block_days, n))
    if n == 0:
        nan = float("nan")
        return MonteCarloSummary(runs, 0, block, nan, nan, nan, nan, nan, nan, nan, drawdown_limit)
    rng = np.random.default_rng(seed)
    count = -(-n // block)
    starts = rng.integers(0, n - block + 1, size=(runs, count))
    index = (starts[:, :, None] + np.arange(block)).reshape(runs, count * block)[:, :n]
    growth = np.cumprod(1 + returns[index], axis=1)
    path = np.concatenate([np.ones((runs, 1)), growth], axis=1)
    drawdowns = (path / np.maximum.accumulate(path, axis=1) - 1).min(axis=1)
    finals = growth[:, -1] - 1
    dd_p5, dd_p50, dd_p95 = np.percentile(drawdowns, [5, 50, 95])
    r_p5, r_p50, r_p95 = np.percentile(finals, [5, 50, 95])
    return MonteCarloSummary(
        runs=runs,
        days=n,
        block_days=block,
        drawdown_p5=float(dd_p5),
        drawdown_p50=float(dd_p50),
        drawdown_p95=float(dd_p95),
        return_p5=float(r_p5),
        return_p50=float(r_p50),
        return_p95=float(r_p95),
        prob_drawdown_beyond=float(np.mean(drawdowns < -drawdown_limit)),
        drawdown_limit=drawdown_limit,
    )
