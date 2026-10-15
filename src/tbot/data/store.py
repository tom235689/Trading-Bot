"""Parquet bar storage: one file per symbol, timeframe, and year."""

import io
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

REPLACE_ATTEMPTS = 8  # Windows refuses to replace a file another process is reading
READ_ATTEMPTS = 5  # nor can a file be opened at the moment another process replaces it
LOCK_SECONDS = 60.0  # another process merging into the same stream gets this long
DAMAGED = ".damaged"  # suffix of a year file set aside because it cannot be read
REFETCH = ".refetch"  # a year set aside and not fetched again yet
# Polars panics instead of raising on some damaged files; a panic is a BaseException.
PARSE_ERRORS = (pl.exceptions.PolarsError, pl.exceptions.PanicException)


class StoreBusy(OSError):
    """Another process kept a stream locked for longer than the writer could wait."""


class StoreUnreadable(OSError):
    """A stored year file that cannot be read: damaged (it does not parse), or held."""

    def __init__(self, message: str, path: Path, *, damaged: bool) -> None:
        super().__init__(message)
        self.path = path
        self.damaged = damaged


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

    def _load(
        self, path: Path, symbol: str, timeframe: Timeframe, columns: list[str] | None = None
    ) -> pl.DataFrame:
        """One year file, read in one open: a replace by another process between two reads
        of the same file would tear it. A file just replaced is tried again first."""
        attempt = 0
        while True:
            try:
                return pl.read_parquet(io.BytesIO(path.read_bytes()), columns=columns)
            except FileNotFoundError:  # set aside by another process since it was listed
                return empty_bars().select(columns or BAR_SCHEMA.names())
            except (OSError, *PARSE_ERRORS) as exc:
                attempt += 1
                if attempt < READ_ATTEMPTS:
                    time.sleep(0.025 * 2**attempt)
                    continue
                if isinstance(exc, OSError):
                    problem = f"cannot read {path} ({exc}); another program may hold it open"
                    raise StoreUnreadable(problem, path, damaged=False) from exc
                hint = self.download_hint([(symbol, timeframe)])
                error = " ".join(str(exc).split())[:200]
                problem = (
                    f"{path} is damaged ({error}); {hint} sets it aside and fetches that year again"
                )
                raise StoreUnreadable(problem, path, damaged=True) from exc

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
        bars = pl.concat([self._load(file, symbol, timeframe) for file in files])
        if start is not None:
            bars = bars.filter(pl.col("open_time") >= start)
        if end is not None:
            bars = bars.filter(pl.col("open_time") < end)
        return bars.sort("open_time")

    def first_open_time(self, symbol: str, timeframe: Timeframe) -> datetime | None:
        for file in self._files(symbol, timeframe):
            value = self._load(file, symbol, timeframe, ["open_time"])["open_time"].min()
            if isinstance(value, datetime):
                return value
        return None

    def last_open_time(self, symbol: str, timeframe: Timeframe) -> datetime | None:
        for file in reversed(self._files(symbol, timeframe)):
            value = self._load(file, symbol, timeframe, ["open_time"])["open_time"].max()
            if isinstance(value, datetime):
                return value
        return None

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
                    part = pl.concat([self._load(file, symbol, timeframe), part])
                merged = part.unique("open_time", keep="last", maintain_order=True)
                # Write then rename so a crash never leaves a partial file.
                tmp = file.with_suffix(f".{os.getpid()}.tmp")
                merged.sort("open_time").write_parquet(tmp)
                with tmp.open("rb+") as handle:  # on disk before it replaces the old file
                    os.fsync(handle.fileno())
                try:
                    _replace(tmp, file)
                except OSError:
                    tmp.unlink(missing_ok=True)
                    raise

    def set_aside(self, path: Path) -> Path | None:
        """Rename a damaged year file out of the way (it is kept, not deleted) and mark its
        year to be fetched again. Return the new path, or None if another process has
        replaced the file since."""
        directory = path.parent
        with locked(directory / ".lock"):
            try:  # no writer runs now: one read says whether it is still damaged
                pl.read_parquet(io.BytesIO(path.read_bytes()))
                return None
            except FileNotFoundError:
                return None
            except PARSE_ERRORS:
                pass
            target = path.with_name(path.name + DAMAGED)
            copies = 1
            while target.exists():  # damaged before: keep every copy
                copies += 1
                target = path.with_name(f"{path.name}{DAMAGED}{copies}")
            # The mark goes first: a crash in between costs a fetch, never a hole.
            (directory / f"{path.stem}{REFETCH}").touch()
            _replace(path, target)
        return target

    def years_to_refetch(self, symbol: str, timeframe: Timeframe) -> list[int]:
        """Years whose file was set aside and has not been fetched again."""
        marks = self.directory(symbol, timeframe).glob(f"*{REFETCH}")
        return sorted(int(mark.stem) for mark in marks if mark.stem.isdigit())

    def refetched(self, symbol: str, timeframe: Timeframe, year: int) -> None:
        (self.directory(symbol, timeframe) / f"{year}{REFETCH}").unlink(missing_ok=True)


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
                raise
            time.sleep(min(0.05 * 2**attempt, 1.0))
