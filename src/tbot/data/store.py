"""Parquet bar storage: one file per symbol, timeframe, and year."""

import os
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import polars as pl

from tbot.core.filelock import lock_file
from tbot.core.timeframe import Timeframe
from tbot.data.schema import BAR_SCHEMA, empty_bars

REPLACE_ATTEMPTS = 6  # Windows refuses to replace a file another process is reading
LOCK_SECONDS = 60.0  # another process merging into the same stream gets this long


class StoreBusy(OSError):
    """Another process kept a stream locked for longer than the writer could wait."""


class BarStore:
    def __init__(self, root: Path, exchange: str = "binance", market: str = "spot") -> None:
        self.base = root
        self.root = root / exchange / market / "klines"

    def download_hint(self, streams: Iterable[tuple[str, Timeframe]]) -> str:
        """The `tbot download` that fetches these streams into this store."""
        pairs = list(streams)
        symbols = " ".join(dict.fromkeys(symbol for symbol, _ in pairs))
        frames = " ".join(str(t) for t in sorted({t for _, t in pairs}, key=lambda t: t.millis))
        where = "" if self.base == Path("data") else f' --data-dir "{self.base}"'
        return f"`tbot download --symbols {symbols} --timeframes {frames}{where}`"

    def directory(self, symbol: str, timeframe: Timeframe) -> Path:
        return self.root / symbol / str(timeframe)

    def _files(self, symbol: str, timeframe: Timeframe) -> list[Path]:
        return sorted(self.directory(symbol, timeframe).glob("*.parquet"))

    def read(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> pl.DataFrame:
        """Bars with open_time in [start, end), sorted by open_time."""
        files = self._files(symbol, timeframe)
        if not files:
            return empty_bars()
        bars = pl.read_parquet(files)
        if start is not None:
            bars = bars.filter(pl.col("open_time") >= start)
        if end is not None:
            bars = bars.filter(pl.col("open_time") < end)
        return bars.sort("open_time")

    def first_open_time(self, symbol: str, timeframe: Timeframe) -> datetime | None:
        files = self._files(symbol, timeframe)
        if not files:
            return None
        value = pl.read_parquet(files[0], columns=["open_time"])["open_time"].min()
        return value if isinstance(value, datetime) else None

    def last_open_time(self, symbol: str, timeframe: Timeframe) -> datetime | None:
        files = self._files(symbol, timeframe)
        if not files:
            return None
        value = pl.read_parquet(files[-1], columns=["open_time"])["open_time"].max()
        return value if isinstance(value, datetime) else None

    def write(
        self,
        symbol: str,
        timeframe: Timeframe,
        bars: pl.DataFrame,
        *,
        newest_first: bool = False,
        wait: float = LOCK_SECONDS,
    ) -> None:
        """Merge bars into storage. On duplicate open_time the new row wins.

        Year files are written oldest first, so a failure leaves the stored range unbroken
        for the next forward sync; a backfill passes newest_first for the same reason.
        """
        if bars.schema != BAR_SCHEMA:
            raise ValueError(f"unexpected schema: {bars.schema}")
        if bars.is_empty():
            return
        directory = self.directory(symbol, timeframe)
        directory.mkdir(parents=True, exist_ok=True)
        years = bars["open_time"].dt.year().unique().sort(descending=newest_first)
        with locked(directory / ".lock", wait):  # a merge by another process would be lost
            for stale in directory.glob("*.tmp"):  # left by a writer that died; none runs now
                stale.unlink(missing_ok=True)
            for year in years:
                file = directory / f"{year}.parquet"
                part = bars.filter(pl.col("open_time").dt.year() == year)
                if file.exists():
                    part = pl.concat([pl.read_parquet(file), part])
                merged = part.unique("open_time", keep="last", maintain_order=True)
                # Write then rename so a crash never leaves a partial file.
                tmp = file.with_suffix(f".{os.getpid()}.tmp")
                merged.sort("open_time").write_parquet(tmp)
                with tmp.open("rb+") as handle:  # on disk before it replaces the old file
                    os.fsync(handle.fileno())
                _replace(tmp, file)


@contextmanager
def locked(path: Path, wait: float = LOCK_SECONDS) -> Iterator[None]:
    """An exclusive lock between processes, released by the system if one dies."""
    with path.open("a+") as handle:
        deadline = time.monotonic() + wait
        while True:
            try:
                lock_file(handle, lock=True)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise StoreBusy(f"{path} stays locked by another process") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            lock_file(handle, lock=False)


def _replace(source: Path, target: Path) -> None:
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            source.replace(target)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                source.unlink(missing_ok=True)
                raise
            time.sleep(0.05 * 2**attempt)
