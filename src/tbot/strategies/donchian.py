"""Donchian channel trend following."""

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tbot.strategies.base import Strategy, StrategyContext
from tbot.strategies.registry import register_strategy


class DonchianParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entry: int = Field(default=55, ge=2)
    exit: int = Field(default=20, ge=2)


@register_strategy
class DonchianTrend(Strategy):
    """Go long when the close breaks above the prior `entry`-bar high; exit when it
    breaks below the prior `exit`-bar low. Capital is split equally across symbols."""

    name = "donchian_trend"

    def __init__(self, symbols: Sequence[str], params: Mapping[str, Any] | None = None) -> None:
        super().__init__(symbols)
        self.params = DonchianParams.model_validate(params or {})
        self._long = dict.fromkeys(self.symbols, False)

    @property
    def warmup(self) -> int:
        return max(self.params.entry, self.params.exit) + 1

    def on_bar(self, ctx: StrategyContext) -> dict[str, float]:
        weight = 1.0 / len(self.symbols)
        for symbol in self.symbols:
            bars = ctx.bars(symbol)
            if len(bars) < self.warmup:
                continue
            close = bars.close[-1]
            if close > bars.high[-self.params.entry - 1 : -1].max():
                self._long[symbol] = True
            elif close < bars.low[-self.params.exit - 1 : -1].min():
                self._long[symbol] = False
        return {symbol: weight if self._long[symbol] else 0.0 for symbol in self.symbols}
