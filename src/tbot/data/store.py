"""Parquet bar storage: one file per symbol, timeframe, and year."""

from datetime import datetime
from pathlib import Path

import polars as pl

from tbot.core.timeframe import Timeframe
from tbot.data.schema import BAR_SCHEMA, empty_bars


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

    def last_open_time(self, symbol: str, timeframe: Timeframe) -> datetime | None:
        files = self._files(symbol, timeframe)
        if not files:
            return None
        value = pl.read_parquet(files[-1], columns=["open_time"])["open_time"].max()
        return value if isinstance(value, datetime) else None

    def write(self, symbol: str, timeframe: Timeframe, bars: pl.DataFrame) -> None:
        """Merge bars into storage. On duplicate open_time the new row wins."""
        if bars.schema != BAR_SCHEMA:
            raise ValueError(f"unexpected schema: {bars.schema}")
        if bars.is_empty():
            return
        directory = self.directory(symbol, timeframe)
        directory.mkdir(parents=True, exist_ok=True)
        for year in bars["open_time"].dt.year().unique().sort():
            file = directory / f"{year}.parquet"
            part = bars.filter(pl.col("open_time").dt.year() == year)
            if file.exists():
                part = pl.concat([pl.read_parquet(file), part])
            merged = part.unique("open_time", keep="last", maintain_order=True).sort("open_time")
            # Write then rename so a crash never leaves a partial file.
            tmp = file.with_suffix(".tmp")
            merged.write_parquet(tmp)
            tmp.replace(file)
