"""Sync stored bars with Binance: monthly archives first, REST for the recent tail."""

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import groupby

import httpx
import polars as pl
import structlog

from tbot.core.timeframe import Timeframe
from tbot.data.binance_rest import fetch_klines, first_open_time
from tbot.data.binance_vision import fetch_month
from tbot.data.quality import aligned
from tbot.data.store import BarStore

log = structlog.get_logger(__name__)
INVALID_SYMBOL = 400  # what the REST API answers for a symbol it no longer lists


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
    """Download closed bars from start up to now, without holes.

    Extends forward from the last stored bar, and backfills bars before the first
    stored one when start is earlier.
    """
    counts = [0, 0, 0]  # archive bars, REST bars, dropped
    first = store.first_open_time(symbol, timeframe)
    last = store.last_open_time(symbol, timeframe)
    if first is not None and start < first:
        # One probe first, so a start before the listing costs a request, not a download.
        try:
            earliest = first_open_time(client, symbol, timeframe, start, first)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != INVALID_SYMBOL:
                raise
            earliest = start  # delisted: only the archives can say what there was
        if earliest is not None:
            _download(
                store,
                client,
                symbol,
                timeframe,
                earliest,
                first,
                workers,
                progress,
                counts,
                hold=True,
            )
    begin = last + timeframe.delta if last is not None else start
    _download(
        store, client, symbol, timeframe, begin, timeframe.floor(now), workers, progress, counts
    )

    stored = store.read(symbol, timeframe)["open_time"]
    return SyncResult(
        symbol=symbol,
        timeframe=timeframe,
        archive_bars=counts[0],
        rest_bars=counts[1],
        dropped=counts[2],
        total_bars=stored.len(),
        first=stored[0] if stored.len() else None,
        last=stored[-1] if stored.len() else None,
    )


def _download(
    store: BarStore,
    client: httpx.Client,
    symbol: str,
    timeframe: Timeframe,
    begin: datetime,
    end: datetime,
    workers: int,
    progress: Callable[[str], None],
    counts: list[int],
    *,
    hold: bool = False,
) -> None:
    """Store bars with open_time in [begin, end): archives for complete months, then REST.

    With hold, nothing is written until everything is fetched: a backfill that fails
    halfway would otherwise leave a hole the next sync cannot see. A forward sync holds
    from the first month missing from the archives until REST has filled it, for the
    same reason.
    """
    if begin >= end:
        return
    held: list[pl.DataFrame] = []
    holding = hold

    def keep(bars: pl.DataFrame) -> tuple[int, int]:
        if not holding:
            return store_aligned(store, symbol, timeframe, bars)
        kept = bars.filter(aligned(timeframe))
        held.append(kept)
        return kept.height, bars.height - kept.height

    def fetch(year_month: tuple[int, int]) -> pl.DataFrame | None:
        return fetch_month(client, symbol, timeframe, *year_month)

    # Complete months come from archives, one year at a time to bound memory.
    covered = begin  # everything before this is stored
    unpublished: list[tuple[int, int]] = []
    months = iter_months(begin.date(), end.date())
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for year, year_months in groupby(months, key=lambda ym: ym[0]):
            wanted = list(year_months)
            frames = []
            for month, frame in zip(wanted, pool.map(fetch, wanted), strict=True):
                if frame is None:
                    unpublished.append(month)
                    holding = True  # nothing after this hole is stored before it is filled
                else:
                    frames.append(frame)
            if not frames:
                continue
            bars = pl.concat(frames).filter(pl.col("open_time") >= begin, pl.col("open_time") < end)
            written, skipped = keep(bars)
            counts[0] += written
            counts[2] += skipped
            latest = bars["open_time"].max()
            if isinstance(latest, datetime):
                covered = max(covered, latest + timeframe.delta)
            progress(f"{symbol} {timeframe} {year}: {written} bars from archives")

    # REST fills months missing between archives, then everything after the last
    # archived bar, which covers months not yet published.
    ranges = [
        (max(month_start, begin), month_end)
        for month_start, month_end in map(_month_span, unpublished)
        if month_end <= covered
    ]
    if covered < end:
        ranges.append((covered, end))
    for range_start, range_end in ranges:
        try:
            bars = fetch_klines(client, symbol, timeframe, range_start, range_end)
        except httpx.HTTPStatusError as exc:
            archived = counts[0] or store.last_open_time(symbol, timeframe) is not None
            if exc.response.status_code != INVALID_SYMBOL or not archived:
                raise  # a symbol that never existed is a mistake to report
            # Archived, but no longer listed: the archives are all there is.
            log.warning("symbol_not_listed", symbol=symbol, after=range_start.isoformat())
            progress(f"{symbol} {timeframe}: not listed any more; archives only")
            break
        written, skipped = keep(bars)
        counts[1] += written
        counts[2] += skipped
        progress(f"{symbol} {timeframe}: {written} bars from REST")
    if held:
        store.write(symbol, timeframe, pl.concat(held), newest_first=hold)
