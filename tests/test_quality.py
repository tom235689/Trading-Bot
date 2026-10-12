from datetime import UTC, datetime, timedelta

import polars as pl

from binance_fake import make_bars
from tbot.core.timeframe import Timeframe
from tbot.data.quality import Gap, check_bars

H1 = Timeframe.H1
T0 = datetime(2024, 1, 1, tzinfo=UTC)
LATER = datetime(2025, 1, 1, tzinfo=UTC)
BARS = make_bars(T0, 48, H1)
ROW = pl.int_range(pl.len())


def at(hours: int) -> datetime:
    return T0 + timedelta(hours=hours)


def test_clean_data() -> None:
    report = check_bars(BARS, H1, LATER)
    assert report.ok
    assert (report.bars, report.first, report.last) == (48, T0, at(47))
    assert report.gaps == ()
    assert report.large_moves == ()


def test_gap_is_warning() -> None:
    report = check_bars(BARS.filter(~ROW.is_between(10, 12)), H1, LATER)
    assert report.ok
    assert report.gaps == (Gap(start=at(10), end=at(12), missing=3, step=timedelta(hours=1)),)
    assert report.gaps[0].length == timedelta(hours=3)
    assert report.missing_bars == 3


def test_duplicates_fail() -> None:
    report = check_bars(pl.concat([BARS, BARS.head(1)]), H1, LATER)
    assert (report.ok, report.duplicates) == (False, 1)


def test_invalid_values_fail() -> None:
    bars = BARS.with_columns(high=pl.when(ROW == 5).then(0.5).otherwise(pl.col("high")))
    report = check_bars(bars, H1, LATER)
    assert (report.ok, report.invalid_values) == (False, 1)


def test_misaligned_bars_fail() -> None:
    report = check_bars(make_bars(T0 + timedelta(minutes=1), 5, H1), H1, LATER)
    assert (report.ok, report.misaligned) == (False, 5)


def test_unclosed_bar_fails() -> None:
    report = check_bars(BARS, H1, at(47) + timedelta(minutes=30))
    assert (report.ok, report.incomplete) == (False, 1)


def test_zero_volume_and_large_moves_are_warnings() -> None:
    bars = BARS.with_columns(
        volume=pl.when(ROW.is_in([3, 4])).then(0.0).otherwise(pl.col("volume")),
        close=pl.when(ROW == 20).then(200.0).otherwise(pl.col("close")),
        high=pl.when(ROW == 20).then(201.0).otherwise(pl.col("high")),
    )
    report = check_bars(bars, H1, LATER)
    assert report.ok
    assert report.zero_volume == 2
    assert report.large_moves == (at(20), at(21))


def test_nan_and_negative_values_fail() -> None:
    bars = BARS.with_columns(
        close=pl.when(ROW == 2).then(float("nan")).otherwise(pl.col("close")),
        high=pl.when(ROW == 3).then(float("inf")).otherwise(pl.col("high")),
        volume=pl.when(ROW == 4).then(-10.0).otherwise(pl.col("volume")),
    )
    report = check_bars(bars, H1, LATER)
    assert (report.ok, report.invalid_values) == (False, 3)


def test_a_week_without_bars_is_missing_data() -> None:
    bars = make_bars(T0, 480, H1)
    week = bars.filter((pl.col("open_time") < at(100)) | (pl.col("open_time") >= at(100 + 24 * 8)))
    report = check_bars(week, H1, LATER)
    assert not report.ok
    assert len(report.long_gaps) == 1
    days = bars.filter((pl.col("open_time") < at(100)) | (pl.col("open_time") >= at(100 + 24 * 3)))
    assert check_bars(days, H1, LATER).ok  # Binance paused for days in 2018: a warning


def test_exactly_a_week_of_missing_bars_is_a_long_gap() -> None:
    bars = make_bars(T0, 480, H1)

    def without(hours: int) -> bool:
        kept = (pl.col("open_time") < at(100)) | (pl.col("open_time") >= at(100 + hours))
        return check_bars(bars.filter(kept), H1, LATER).ok

    assert not without(24 * 7)  # 168 bars missing: a week
    assert without(24 * 7 - 1)
