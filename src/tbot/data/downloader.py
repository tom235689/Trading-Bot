"""Sync stored bars with Binance: monthly archives first, REST for the recent tail."""

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime
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
    """Download closed bars from start (or the last stored bar) up to now.

    Only extends forward. To backfill earlier history, delete the stored data first.
    """
    last = store.last_open_time(symbol, timeframe)
    begin = last if last is not None else start

    def fetch(year_month: tuple[int, int]) -> pl.DataFrame | None:
        return fetch_month(client, symbol, timeframe, *year_month)

    # Complete months come from archives, one year at a time to bound memory.
    archive_bars = dropped = 0
    months = iter_months(begin.date(), now.date())
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for year, year_months in groupby(months, key=lambda ym: ym[0]):
            frames = [f for f in pool.map(fetch, year_months) if f is not None]
            if not frames:
                continue
            bars = pl.concat(frames).filter(pl.col("open_time") >= start)
            written, skipped = store_aligned(store, symbol, timeframe, bars)
            archive_bars, dropped = archive_bars + written, dropped + skipped
            progress(f"{symbol} {timeframe} {year}: {written} bars from archives")

    # Anything after the last archived bar, including unpublished months, comes from REST.
    last = store.last_open_time(symbol, timeframe)
    tail_start = max(last + timeframe.delta, start) if last is not None else start
    tail_end = timeframe.floor(now)
    rest_bars = 0
    if tail_start < tail_end:
        bars = fetch_klines(client, symbol, timeframe, tail_start, tail_end)
        rest_bars, skipped = store_aligned(store, symbol, timeframe, bars)
        dropped += skipped
        progress(f"{symbol} {timeframe}: {rest_bars} recent bars from REST")

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
