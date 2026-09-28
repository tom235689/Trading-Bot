from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from factories import price_bars
from tbot.core.timeframe import Timeframe
from tbot.live.history import BarHistory

H1 = Timeframe.H1
T0 = datetime(2024, 1, 1, tzinfo=UTC)
BARS = price_bars(T0, H1, [1.0, 2.0, 3.0], [1.5, 2.5, 3.5])


def test_append_and_window() -> None:
    history = BarHistory(H1)
    history.append(BARS.head(2))
    history.append(BARS.tail(1))
    assert len(history) == 3
    assert history.last_open_time == T0 + timedelta(hours=2)
    assert history.last_close == 3.5

    window = history.window()
    assert window.close.tolist() == [1.5, 2.5, 3.5]
    assert window.open_time[-1] == np.datetime64("2024-01-01T02:00")
    with pytest.raises(ValueError, match="read-only"):
        window.close[0] = 0.0


def test_rejects_out_of_order_and_duplicates() -> None:
    history = BarHistory(H1, BARS)
    with pytest.raises(ValueError, match="increasing"):
        history.append(BARS.tail(1))
    with pytest.raises(ValueError, match="duplicate"):
        BarHistory(H1).append(pl.concat([BARS, BARS.head(1)]))


def test_keeps_only_recent_bars() -> None:
    history = BarHistory(H1, BARS, max_bars=2)
    assert history.window().close.tolist() == [2.5, 3.5]
    assert BarHistory(H1).last_open_time is None
