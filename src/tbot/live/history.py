"""Growing history of closed bars for one stream."""

from datetime import datetime

import numpy as np
import numpy.typing as npt
import polars as pl

from tbot.core.arrays import readonly
from tbot.core.timeframe import Timeframe, from_millis
from tbot.strategies.base import BarWindow

COLUMNS = ("open", "high", "low", "close", "volume")
MAX_BARS = 5000  # default history kept per stream


class BarHistory:
    """Closed bars oldest first. Only the most recent max_bars are kept."""

    def __init__(
        self, timeframe: Timeframe, bars: pl.DataFrame | None = None, max_bars: int = MAX_BARS
    ) -> None:
        self.timeframe = timeframe
        self.max_bars = max_bars
        self.open_ms: npt.NDArray[np.int64] = np.zeros(0, dtype=np.int64)
        self.values: dict[str, npt.NDArray[np.float64]] = {c: np.zeros(0) for c in COLUMNS}
        if bars is not None and not bars.is_empty():
            self.append(bars)

    def __len__(self) -> int:
        return len(self.open_ms)

    @property
    def last_open_time(self) -> datetime | None:
        return from_millis(int(self.open_ms[-1])) if len(self) else None

    @property
    def last_close(self) -> float:
        return float(self.values["close"][-1])

    def append(self, bars: pl.DataFrame) -> None:
        """Append bars in order; bars at or before the last stored one are rejected."""
        bars = bars.sort("open_time")
        open_ms = bars["open_time"].dt.epoch("ms").to_numpy().astype(np.int64)
        if len(self) and len(open_ms) and open_ms[0] <= self.open_ms[-1]:
            raise ValueError("bars must be appended in increasing open_time order")
        if len(np.unique(open_ms)) != len(open_ms):
            raise ValueError("duplicate open_time in appended bars")
        keep = slice(-self.max_bars, None)
        self.open_ms = np.concatenate([self.open_ms, open_ms])[keep]
        for column in COLUMNS:
            self.values[column] = np.concatenate([self.values[column], bars[column].to_numpy()])[
                keep
            ]

    def window(self) -> BarWindow:
        return BarWindow(
            open_time=readonly(self.open_ms.astype("datetime64[ms]")),
            open=readonly(self.values["open"]),
            high=readonly(self.values["high"]),
            low=readonly(self.values["low"]),
            close=readonly(self.values["close"]),
            volume=readonly(self.values["volume"]),
        )
