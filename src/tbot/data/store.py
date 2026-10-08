"""Parquet bar storage: one file per symbol, timeframe, and year."""

import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import polars as pl

from tbot.core.timeframe import Timeframe
from tbot.data.schema import BAR_SCHEMA, empty_bars

REPLACE_ATTEMPTS = 6  # Windows refuses to replace a file another process is reading
LOCK_SECONDS = 60.0  # another process merging into the same stream gets this long


class BarStore:
    def __init__(self, root: Path, exchange: str = "binance", market: str = "spot") -> None:
        self.root = root / exchange / market / "klines"

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
        self, symbol: str, timeframe: Timeframe, bars: pl.DataFrame, *, newest_first: bool = False
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
        with _locked(directory / ".lock"):  # a merge by another process would be lost
            for year in years:
                file = directory / f"{year}.parquet"
                part = bars.filter(pl.col("open_time").dt.year() == year)
                if file.exists():
                    part = pl.concat([pl.read_parquet(file), part])
                merged = part.unique("open_time", keep="last", maintain_order=True)
                # Write then rename so a crash never leaves a partial file.
                tmp = file.with_suffix(f".{os.getpid()}.tmp")
                merged.sort("open_time").write_parquet(tmp)
                _replace(tmp, file)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """An exclusive lock between processes, released by the system if one dies."""
    with path.open("a+") as handle:
        deadline = time.monotonic() + LOCK_SECONDS
        while True:
            try:
                _lock(handle.fileno(), lock=True)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{path} stays locked by another process") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            _lock(handle.fileno(), lock=False)


def _lock(fd: int, *, lock: bool) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK if lock else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, (fcntl.LOCK_EX | fcntl.LOCK_NB) if lock else fcntl.LOCK_UN)


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
