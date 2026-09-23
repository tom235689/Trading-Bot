from datetime import UTC, datetime, timedelta

import pytest

from tbot.core.timeframe import EPOCH, Timeframe, to_millis


@pytest.mark.parametrize(
    ("timeframe", "delta"),
    [
        (Timeframe.M1, timedelta(minutes=1)),
        (Timeframe.H4, timedelta(hours=4)),
        (Timeframe.D1, timedelta(days=1)),
    ],
)
def test_delta_and_millis(timeframe: Timeframe, delta: timedelta) -> None:
    assert timeframe.delta == delta
    assert timeframe.millis == delta.total_seconds() * 1000


@pytest.mark.parametrize(
    ("timeframe", "expected"),
    [
        (Timeframe.H1, datetime(2024, 3, 15, 10, tzinfo=UTC)),
        (Timeframe.H4, datetime(2024, 3, 15, 8, tzinfo=UTC)),
        (Timeframe.D1, datetime(2024, 3, 15, tzinfo=UTC)),
    ],
)
def test_floor(timeframe: Timeframe, expected: datetime) -> None:
    assert timeframe.floor(datetime(2024, 3, 15, 10, 30, tzinfo=UTC)) == expected


def test_parse() -> None:
    assert Timeframe("4h") is Timeframe.H4
    with pytest.raises(ValueError, match="1w"):
        Timeframe("1w")


def test_to_millis() -> None:
    assert to_millis(EPOCH + timedelta(seconds=1)) == 1000
