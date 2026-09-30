"""Sync stored bars with Binance: monthly archives first, REST for the recent tail."""

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import groupby

import httpx
import polars as pl

from tbot.core.timeframe import Timeframe
from tbot.data.binance_rest import fetch_klines
from tbot.data.binance_vision import fetch_month
from tbot.data.quality import aligned
from tbot.data.store import BarStore


@dataclass(frozen=True)
class SyncResult:
    symbol: str
    timeframe: Timeframe
    archive_bars: int
    rest_bars: int
    dropped: int  # misaligned bars discarded
    total_bars: int
    first: datetime | None
    last: datetime | None


def iter_months(first: date, stop: date) -> Iterator[tuple[int, int]]:
    """Yield (year, month) from first's month up to, not including, stop's month."""
    year, month = first.year, first.month
    while (year, month) < (stop.year, stop.month):
        yield year, month
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def _month_span(year_month: tuple[int, int]) -> tuple[datetime, datetime]:
    year, month = year_month
    first = datetime(year, month, 1, tzinfo=UTC)
    following = (
        datetime(year + 1, 1, 1, tzinfo=UTC) if month == 12 else first.replace(month=month + 1)
    )
    return first, following


def store_aligned(
    store: BarStore, symbol: str, timeframe: Timeframe, bars: pl.DataFrame
) -> tuple[int, int]:
    """Write only grid-aligned bars; return (written, dropped).

    Binance has stretches of off-grid bars (e.g. 1h bars at :28 after the Feb 2018 outage).
    Snapping them onto the grid would leak up to one bar of future prices, so they are dropped.
    """
    kept = bars.filter(aligned(timeframe))
    store.write(symbol, timeframe, kept)
    return kept.height, bars.height - kept.height


def sync(
    store: BarStore,
    client: httpx.Client,
    symbol: str,
    timeframe: Timeframe,
    start: datetime,
    now: datetime,
    *,
    workers: int = 8,
    progress: Callable[[str], None] = lambda _: None,
) -> SyncResult:
    """Download closed bars from start, or from the last stored bar, up to now.

    Only extends forward, without holes: a start after the last stored bar does not
    skip the bars in between. To backfill earlier history, delete the stored data first.
    """
    last = store.last_open_time(symbol, timeframe)
    begin = last if last is not None else start

    def fetch(year_month: tuple[int, int]) -> pl.DataFrame | None:
        return fetch_month(client, symbol, timeframe, *year_month)

    # Complete months come from archives, one year at a time to bound memory.
    archive_bars = dropped = 0
    unpublished: list[tuple[int, int]] = []
    months = iter_months(begin.date(), now.date())
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for year, year_months in groupby(months, key=lambda ym: ym[0]):
            wanted = list(year_months)
            frames = []
            for month, frame in zip(wanted, pool.map(fetch, wanted), strict=True):
                if frame is None:
                    unpublished.append(month)
                else:
                    frames.append(frame)
            if not frames:
                continue
            bars = pl.concat(frames).filter(pl.col("open_time") >= begin)
            written, skipped = store_aligned(store, symbol, timeframe, bars)
            archive_bars, dropped = archive_bars + written, dropped + skipped
            progress(f"{symbol} {timeframe} {year}: {written} bars from archives")

    # REST fills months missing between archives, then everything after the last
    # archived bar, which covers months not yet published.
    last = store.last_open_time(symbol, timeframe)
    tail_start = last + timeframe.delta if last is not None else start
    ranges = [
        (max(month_start, begin), month_end)
        for month_start, month_end in map(_month_span, unpublished)
        if month_end <= tail_start
    ]
    tail_end = timeframe.floor(now)
    if tail_start < tail_end:
        ranges.append((tail_start, tail_end))
    rest_bars = 0
    for range_start, range_end in ranges:
        bars = fetch_klines(client, symbol, timeframe, range_start, range_end)
        written, skipped = store_aligned(store, symbol, timeframe, bars)
        rest_bars, dropped = rest_bars + written, dropped + skipped
        progress(f"{symbol} {timeframe}: {written} bars from REST")

    stored = store.read(symbol, timeframe)["open_time"]
    return SyncResult(
        symbol=symbol,
        timeframe=timeframe,
        archive_bars=archive_bars,
        rest_bars=rest_bars,
        dropped=dropped,
        total_bars=stored.len(),
        first=stored[0] if stored.len() else None,
        last=stored[-1] if stored.len() else None,
    )
