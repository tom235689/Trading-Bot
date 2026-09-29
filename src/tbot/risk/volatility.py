"""Realized volatility and the exposure scale that keeps a position near a target level."""

import math

import numpy as np
import numpy.typing as npt

from tbot.core.timeframe import Timeframe

SECONDS_PER_YEAR = 365 * 86400
MIN_BARS = 10  # fewer closes than this: no estimate, full exposure


def bars_per_year(timeframe: Timeframe) -> float:
    return SECONDS_PER_YEAR / timeframe.delta.total_seconds()


def lookback_bars(timeframe: Timeframe, days: float) -> int:
    return max(2, round(days * 86400 / timeframe.delta.total_seconds()))


def realized_volatility(closes: npt.ArrayLike, timeframe: Timeframe) -> float:
    """Annualized standard deviation of log returns; nan with fewer than three closes."""
    values = np.asarray(closes, dtype=np.float64)
    if len(values) < 3 or np.any(values <= 0):
        return math.nan
    returns = np.diff(np.log(values))
    return float(returns.std(ddof=1) * math.sqrt(bars_per_year(timeframe)))


def volatility_scale(
    closes: npt.ArrayLike, timeframe: Timeframe, target: float, lookback_days: float
) -> float:
    """Fraction of the target weight to hold so the position runs near `target` volatility.

    Never above 1: spot cannot lever up. Full exposure while history is too short.
    """
    if target <= 0:
        return 1.0
    values = np.asarray(closes, dtype=np.float64)
    window = values[-(lookback_bars(timeframe, lookback_days) + 1) :]
    if len(window) < MIN_BARS + 1:
        return 1.0
    realized = realized_volatility(window, timeframe)
    if not math.isfinite(realized) or realized <= 0:
        return 1.0
    return min(1.0, target / realized)
