"""RSI mean reversion: buy oversold, sell overbought. Long only."""

from collections.abc import Mapping, Sequence
from typing import Any, Self

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tbot.strategies.base import Strategy, StrategyContext
from tbot.strategies.registry import register_strategy

LOOKBACK_PERIODS = 10  # Wilder smoothing over this many periods: a fixed, reproducible window


class RsiParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    period: int = Field(default=14, ge=2)
    low: float = Field(default=30, gt=0, lt=100)
    high: float = Field(default=70, gt=0, lt=100)
    trend_period: int = Field(default=0, ge=0)  # buy only above this SMA; 0 disables

    @model_validator(mode="after")
    def check_bands(self) -> Self:
        if self.low >= self.high:
            raise ValueError("low must be below high")
        return self


def rsi(closes: npt.NDArray[np.float64], period: int) -> float:
    """Wilder's RSI of the last close, seeded with the first `period` changes.

    Uses exactly the closes given, so callers must pass a fixed-length window.
    """
    changes = np.diff(closes)
    if len(changes) < period:
        raise ValueError("not enough closes for the RSI period")
    gains = np.maximum(changes, 0.0)
    losses = np.maximum(-changes, 0.0)
    avg_gain = float(gains[:period].mean())
    avg_loss = float(losses[:period].mean())
    for gain, loss in zip(gains[period:], losses[period:], strict=True):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


@register_strategy
class RsiReversion(Strategy):
    """Go long when RSI drops below `low` (and the close is above the trend SMA, if set);
    exit when RSI rises above `high`. Capital is split equally across symbols."""

    name = "rsi_reversion"

    def __init__(self, symbols: Sequence[str], params: Mapping[str, Any] | None = None) -> None:
        super().__init__(symbols)
        self.params = RsiParams.model_validate(params or {})
        self._long = dict.fromkeys(self.symbols, False)

    @property
    def lookback(self) -> int:
        return self.params.period * LOOKBACK_PERIODS + 1

    @property
    def warmup(self) -> int:
        return max(self.lookback, self.params.trend_period)

    def on_bar(self, ctx: StrategyContext) -> dict[str, float]:
        weight = 1.0 / len(self.symbols)
        for symbol in self.symbols:
            bars = ctx.bars(symbol)
            if len(bars) < self.warmup:
                continue
            value = rsi(bars.close[-self.lookback :], self.params.period)
            close = bars.close[-1]
            uptrend = (
                self.params.trend_period == 0
                or close > bars.close[-self.params.trend_period :].mean()
            )
            if not self._long[symbol] and value < self.params.low and uptrend:
                self._long[symbol] = True
            elif self._long[symbol] and value > self.params.high:
                self._long[symbol] = False
        return {symbol: weight if self._long[symbol] else 0.0 for symbol in self.symbols}

    def restore(self, targets: Mapping[str, float]) -> None:
        for symbol in self.symbols:
            if symbol in targets:
                self._long[symbol] = targets[symbol] > 0
