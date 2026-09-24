"""Monte Carlo on trade order: how bad could the drawdown have been?"""

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from tbot.backtest.engine import BacktestResult


@dataclass(frozen=True)
class MonteCarloSummary:
    runs: int
    trades: int
    drawdown_p5: float  # 5th percentile of max drawdown (worst tail)
    drawdown_p50: float
    drawdown_p95: float
    return_p5: float
    return_p50: float
    return_p95: float
    prob_drawdown_beyond: float  # share of runs with a drawdown worse than the limit
    drawdown_limit: float


def trade_returns_on_equity(result: BacktestResult) -> npt.NDArray[np.float64]:
    """Each closed trade's PnL as a fraction of equity when it was opened."""
    if result.trades.is_empty():
        return np.zeros(0)
    curve = result.equity.select("time", "equity").sort("time")
    joined = result.trades.sort("entry_time").join_asof(
        curve, left_on="entry_time", right_on="time", strategy="backward"
    )
    equity_at_entry = joined["equity"].fill_null(result.initial_cash)
    return (joined["pnl"] / equity_at_entry).to_numpy()


def simulate(
    returns: npt.NDArray[np.float64],
    *,
    runs: int = 2000,
    seed: int = 1,
    replace: bool = False,
    drawdown_limit: float = 0.25,
) -> MonteCarloSummary:
    """Compound trade returns in random order (or bootstrapped with replacement)."""
    n = len(returns)
    if n == 0:
        nan = float("nan")
        return MonteCarloSummary(runs, 0, nan, nan, nan, nan, nan, nan, nan, drawdown_limit)
    rng = np.random.default_rng(seed)
    if replace:
        samples = rng.choice(returns, size=(runs, n), replace=True)
    else:
        samples = rng.permuted(np.tile(returns, (runs, 1)), axis=1)
    growth = np.cumprod(1 + samples, axis=1)
    path = np.concatenate([np.ones((runs, 1)), growth], axis=1)
    drawdowns = (path / np.maximum.accumulate(path, axis=1) - 1).min(axis=1)
    finals = growth[:, -1] - 1
    dd_p5, dd_p50, dd_p95 = np.percentile(drawdowns, [5, 50, 95])
    r_p5, r_p50, r_p95 = np.percentile(finals, [5, 50, 95])
    return MonteCarloSummary(
        runs=runs,
        trades=n,
        drawdown_p5=float(dd_p5),
        drawdown_p50=float(dd_p50),
        drawdown_p95=float(dd_p95),
        return_p5=float(r_p5),
        return_p50=float(r_p50),
        return_p95=float(r_p95),
        prob_drawdown_beyond=float(np.mean(drawdowns < -drawdown_limit)),
        drawdown_limit=drawdown_limit,
    )
