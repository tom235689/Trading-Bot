"""Bar timeframes supported by Binance klines."""

from datetime import UTC, datetime, timedelta
from enum import StrEnum

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def to_millis(moment: datetime) -> int:
    return (moment - EPOCH) // timedelta(milliseconds=1)


def from_millis(millis: int) -> datetime:
    return EPOCH + timedelta(milliseconds=millis)


class Timeframe(StrEnum):
    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H2 = "2h"
    H4 = "4h"
    H6 = "6h"
    H8 = "8h"
    H12 = "12h"
    D1 = "1d"

    @property
    def delta(self) -> timedelta:
        return timedelta(seconds=int(self.value[:-1]) * _UNIT_SECONDS[self.value[-1]])

    @property
    def millis(self) -> int:
        return self.delta // timedelta(milliseconds=1)

    def floor(self, moment: datetime) -> datetime:
        """Open time of the bar containing moment. Every timeframe divides a day."""
        return moment - (moment - EPOCH) % self.delta
