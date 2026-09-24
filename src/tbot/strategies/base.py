"""Strategy plugin interface."""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True)
class BarWindow:
    """Closed bars of one symbol, oldest first. Arrays are read-only."""

    open_time: npt.NDArray[np.datetime64]
    open: FloatArray
    high: FloatArray
    low: FloatArray
    close: FloatArray
    volume: FloatArray

    def __len__(self) -> int:
        return len(self.close)


@dataclass(frozen=True)
class StrategyContext:
    time: datetime  # close time of the latest bars
    windows: Mapping[str, BarWindow]
    exposures: Mapping[str, float]  # current portfolio exposure per symbol

    def bars(self, symbol: str) -> BarWindow:
        return self.windows[symbol]

    def exposure(self, symbol: str) -> float:
        return self.exposures.get(symbol, 0.0)


class Strategy(ABC):
    """Base class for strategy plugins.

    Contract: on_bar sees only closed bars, does no I/O, and is deterministic.
    Internal state is allowed if it derives only from bars passed to on_bar,
    so replaying history rebuilds it. Subclasses validate their own params.
    """

    name: ClassVar[str]

    def __init__(self, symbols: Sequence[str], params: Mapping[str, Any] | None = None) -> None:
        if not symbols:
            raise ValueError(f"{self.name}: at least one symbol is required")
        self.symbols = tuple(symbols)

    @property
    @abstractmethod
    def warmup(self) -> int:
        """Bars needed before the first signal."""

    @abstractmethod
    def on_bar(self, ctx: StrategyContext) -> Mapping[str, float]:
        """Target exposure per symbol in [-1, 1], as a fraction of this strategy's capital."""
