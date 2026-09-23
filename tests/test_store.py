from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from binance_fake import make_bars
from tbot.core.timeframe import Timeframe
from tbot.data.store import BarStore

H4 = Timeframe.H4
NEW_YEAR_EVE = datetime(2023, 12, 31, tzinfo=UTC)


def test_write_splits_by_year_and_reads_back(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    bars = make_bars(NEW_YEAR_EVE, 12, H4)
    store.write("BTCUSDT", H4, bars)

    files = sorted(p.name for p in store.directory("BTCUSDT", H4).iterdir())
    assert files == ["2023.parquet", "2024.parquet"]
    assert store.read("BTCUSDT", H4).equals(bars)
    assert store.last_open_time("BTCUSDT", H4) == datetime(2024, 1, 1, 20, tzinfo=UTC)


def test_read_filters_range(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    store.write("BTCUSDT", H4, make_bars(NEW_YEAR_EVE, 12, H4))
    start, end = datetime(2023, 12, 31, 8, tzinfo=UTC), datetime(2024, 1, 1, tzinfo=UTC)
    assert store.read("BTCUSDT", H4, start, end)["open_time"].to_list() == [
        datetime(2023, 12, 31, hour, tzinfo=UTC) for hour in (8, 12, 16, 20)
    ]


def test_write_merges_and_new_rows_win(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    store.write("BTCUSDT", H4, make_bars(NEW_YEAR_EVE, 6, H4))
    update = make_bars(datetime(2023, 12, 31, 16, tzinfo=UTC), 4, H4).with_columns(close=1.0)
    store.write("BTCUSDT", H4, update)

    stored = store.read("BTCUSDT", H4)
    assert stored.height == 8
    assert stored["open_time"].is_unique().all()
    assert stored.filter(pl.col("close") == 1.0).height == 4


def test_empty_store(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    assert store.read("BTCUSDT", H4).is_empty()
    assert store.last_open_time("BTCUSDT", H4) is None


def test_rejects_wrong_schema(tmp_path: Path) -> None:
    bars = make_bars(NEW_YEAR_EVE, 2, H4).drop("trades")
    with pytest.raises(ValueError, match="schema"):
        BarStore(tmp_path).write("BTCUSDT", H4, bars)
