"""Statistics for judging backtest results after multiple testing."""

import math
from statistics import NormalDist

import numpy as np
import numpy.typing as npt

from tbot.backtest.metrics import DAYS_PER_YEAR

EULER_GAMMA = 0.5772156649015329
_NORMAL = NormalDist()


def daily_sharpe(annual_sharpe: float) -> float:
    return annual_sharpe / math.sqrt(DAYS_PER_YEAR)


def moments(returns: npt.NDArray[np.float64]) -> tuple[float, float]:
    """Skewness and (non-excess) kurtosis; normal returns give (0, 3)."""
    centered = returns - returns.mean()
    m2 = float(np.mean(centered**2))
    if m2 == 0:
        return 0.0, 3.0
    return float(np.mean(centered**3) / m2**1.5), float(np.mean(centered**4) / m2**2)


def expected_max_sharpe(trial_variance: float, n_trials: int) -> float:
    """Sharpe the best of n_trials unskilled trials would reach by luck (SR0)."""
    if n_trials < 2 or trial_variance <= 0:
        return 0.0
    quantile = (1 - EULER_GAMMA) * _NORMAL.inv_cdf(1 - 1 / n_trials)
    quantile += EULER_GAMMA * _NORMAL.inv_cdf(1 - 1 / (n_trials * math.e))
    return math.sqrt(trial_variance) * quantile


def deflated_sharpe(
    sharpe: float,
    n_obs: int,
    skew: float,
    kurt: float,
    trial_variance: float,
    n_trials: int,
) -> float:
    """Probability that the true Sharpe exceeds zero after accounting for the
    number of trials (Bailey and Lopez de Prado, 2014). All Sharpe values are
    per observation (daily), not annualized."""
    if n_obs < 2 or not math.isfinite(sharpe):
        return math.nan
    sr0 = expected_max_sharpe(trial_variance, n_trials)
    denominator = math.sqrt(max(1 - skew * sharpe + (kurt - 1) / 4 * sharpe**2, 1e-12))
    return _NORMAL.cdf((sharpe - sr0) * math.sqrt(n_obs - 1) / denominator)
