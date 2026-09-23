"""Quality checks for stored bars.

Errors (duplicates, misaligned or unclosed bars, invalid prices) mean the data is broken.
Warnings (gaps, zero volume, large moves) are usually real market events and are only reported.
"""

from dataclasses import dataclass
from datetime import datetime

import polars as pl

from tbot.core.timeframe import Timeframe


@dataclass(frozen=True)
class Gap:
    start: datetime  # first missing open_time
    end: datetime  # last missing open_time
    missing: int


@dataclass(frozen=True)
class QualityReport:
    bars: int
    first: datetime | None
    last: datetime | None
    duplicates: int
    misaligned: int
    incomplete: int
    invalid_prices: int
    gaps: tuple[Gap, ...]
    zero_volume: int
    large_moves: tuple[datetime, ...]

    @property
    def ok(self) -> bool:
        return not (self.duplicates or self.misaligned or self.incomplete or self.invalid_prices)

    @property
    def missing_bars(self) -> int:
        return sum(gap.missing for gap in self.gaps)


def aligned(timeframe: Timeframe) -> pl.Expr:
    """True for bars whose open_time sits on the timeframe grid."""
    return pl.col("open_time").dt.epoch("ms") % timeframe.millis == 0


def check_bars(
    bars: pl.DataFrame, timeframe: Timeframe, now: datetime, *, move_threshold: float = 0.25
) -> QualityReport:
    """Check bars. move_threshold is the absolute log return that counts as a large move."""
    step = timeframe.delta
    unique = bars.unique("open_time").sort("open_time")
    times = unique["open_time"]

    invalid = (
        (pl.col("high") < pl.max_horizontal("open", "close"))
        | (pl.col("low") > pl.min_horizontal("open", "close"))
        | (pl.col("low") <= 0)
        | pl.any_horizontal(pl.all().is_null())
    )
    gaps = unique.select(prev=pl.col("open_time").shift(1), cur=pl.col("open_time")).filter(
        pl.col("cur") - pl.col("prev") > step
    )
    moves = unique.filter((pl.col("close") / pl.col("close").shift(1)).log().abs() > move_threshold)

    return QualityReport(
        bars=bars.height,
        first=times[0] if times.len() else None,
        last=times[-1] if times.len() else None,
        duplicates=bars.height - unique.height,
        misaligned=unique.filter(~aligned(timeframe)).height,
        incomplete=unique.filter(pl.col("open_time") + step > now).height,
        invalid_prices=bars.filter(invalid).height,
        gaps=tuple(
            Gap(start=prev + step, end=cur - step, missing=(cur - prev) // step - 1)
            for prev, cur in gaps.iter_rows()
        ),
        zero_volume=bars.filter(pl.col("volume") == 0).height,
        large_moves=tuple(moves["open_time"]),
    )
