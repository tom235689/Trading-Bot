"""Test builders for bars and a scripted strategy."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import polars as pl

from tbot.core.timeframe import Timeframe
from tbot.data.schema import BAR_SCHEMA
from tbot.strategies.base import Strategy, StrategyContext


def price_bars(
    start: datetime, timeframe: Timeframe, opens: Sequence[float], closes: Sequence[float]
) -> pl.DataFrame:
    """Consecutive bars from open and close prices; high and low hug them."""
    rows = [
        {
            "open_time": start + timeframe.delta * i,
            "open": o,
            "high": max(o, c),
            "low": min(o, c),
            "close": c,
            "volume": 1.0,
            "quote_volume": c,
            "trades": 1,
            "taker_buy_volume": 0.5,
            "taker_buy_quote_volume": c / 2,
        }
        for i, (o, c) in enumerate(zip(opens, closes, strict=True))
    ]
    return pl.DataFrame(rows, schema=BAR_SCHEMA)


class Scripted(Strategy):
    """Holds preset targets from given close times on, and records every call."""

    name = "scripted"

    def __init__(self, symbols: Sequence[str], params: Mapping[str, Any] | None = None) -> None:
        super().__init__(symbols)
        self.script: dict[datetime, dict[str, float]] = dict((params or {}).get("script", {}))
        self.current: dict[str, float] = {}
        self.calls: list[StrategyContext] = []

    @property
    def warmup(self) -> int:
        return 1

    def on_bar(self, ctx: StrategyContext) -> dict[str, float]:
        self.calls.append(ctx)
        self.current = self.script.get(ctx.time, self.current)
        return self.current
