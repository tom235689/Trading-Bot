from collections.abc import Callable
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

    files = sorted(p.name for p in store.directory("BTCUSDT", H4).glob("*.parquet"))
    assert files == ["2023.parquet", "2024.parquet"]
    assert store.read("BTCUSDT", H4).equals(bars)
    assert store.first_open_time("BTCUSDT", H4) == NEW_YEAR_EVE
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
    assert store.first_open_time("BTCUSDT", H4) is None
    assert store.last_open_time("BTCUSDT", H4) is None


def test_rejects_wrong_schema(tmp_path: Path) -> None:
    bars = make_bars(NEW_YEAR_EVE, 2, H4).drop("trades")
    with pytest.raises(ValueError, match="schema"):
        BarStore(tmp_path).write("BTCUSDT", H4, bars)


def test_write_retries_while_another_process_reads_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BarStore(tmp_path)
    real = Path.replace
    calls: list[Path] = []

    def busy_twice(self: Path, target: Path) -> Path:
        calls.append(target)
        if len(calls) < 3:
            raise PermissionError("in use")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", busy_twice)
    monkeypatch.setattr("tbot.data.store.time.sleep", lambda seconds: None)
    store.write("BTCUSDT", H4, make_bars(datetime(2024, 1, 1, tzinfo=UTC), 3, H4))
    assert len(calls) == 3
    assert store.read("BTCUSDT", H4).height == 3
    assert not list(store.directory("BTCUSDT", H4).glob("*.tmp"))


def test_writers_take_turns(tmp_path: Path) -> None:
    import threading

    from tbot.data.store import locked

    store = BarStore(tmp_path)
    store.write("BTCUSDT", H4, make_bars(NEW_YEAR_EVE, 2, H4))
    other = threading.Thread(
        target=store.write, args=("BTCUSDT", H4, make_bars(NEW_YEAR_EVE, 4, H4).tail(2))
    )
    with locked(store.directory("BTCUSDT", H4) / ".lock"):  # another process is merging
        other.start()
        other.join(0.3)
        assert other.is_alive()  # it waits instead of merging into a stale read
    other.join(5)
    assert store.read("BTCUSDT", H4).height == 4


def test_a_temporary_file_a_dead_writer_left_is_removed(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    directory = store.directory("BTCUSDT", H4)
    directory.mkdir(parents=True)
    stale = directory / "2023.4242.tmp"  # a crash between writing and renaming
    stale.write_bytes(b"partial")
    store.write("BTCUSDT", H4, make_bars(NEW_YEAR_EVE, 3, H4))
    assert not stale.exists()


def test_a_writer_that_cannot_wait_longer_finds_the_store_busy(tmp_path: Path) -> None:
    from tbot.data.store import StoreBusy, locked

    store = BarStore(tmp_path)
    store.write("BTCUSDT", H4, make_bars(NEW_YEAR_EVE, 2, H4))
    with locked(store.directory("BTCUSDT", H4) / ".lock"), pytest.raises(StoreBusy):
        store.write("BTCUSDT", H4, make_bars(NEW_YEAR_EVE, 4, H4), wait=0.1)
    assert not issubclass(StoreBusy, TimeoutError)  # never taken for a silent socket


def test_a_damaged_year_file_is_named_with_the_cure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tbot.data.store import StoreUnreadable

    monkeypatch.setattr("tbot.data.store.time.sleep", lambda seconds: None)
    store = BarStore(tmp_path)
    store.write("BTCUSDT", H4, make_bars(NEW_YEAR_EVE, 12, H4))
    damaged = store.directory("BTCUSDT", H4) / "2023.parquet"
    damaged.write_bytes(damaged.read_bytes()[:-30])  # an interrupted copy
    reads: list[Callable[[], object]] = [
        lambda: store.read("BTCUSDT", H4),
        lambda: store.first_open_time("BTCUSDT", H4),
        lambda: store.write("BTCUSDT", H4, make_bars(NEW_YEAR_EVE, 1, H4)),
    ]
    for read in reads:
        with pytest.raises(StoreUnreadable) as caught:
            read()
        assert (caught.value.path, caught.value.damaged) == (damaged, True)
        assert str(damaged) in str(caught.value)
        assert "tbot download --symbols BTCUSDT --timeframes 4h" in str(caught.value)
    assert store.last_open_time("BTCUSDT", H4) == datetime(2024, 1, 1, 20, tzinfo=UTC)
    assert isinstance(caught.value, OSError)  # each command's errors of one stream catch it


def test_a_read_while_another_process_replaces_the_file_is_tried_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BarStore(tmp_path)
    store.write("BTCUSDT", H4, make_bars(NEW_YEAR_EVE, 3, H4))
    real = pl.read_parquet
    calls = [0]

    def torn(source: object, **kwargs: object) -> pl.DataFrame:
        calls[0] += 1
        if calls[0] == 1:  # the old file's footer, the new file's pages
            raise pl.exceptions.ComputeError("The page header reported the wrong page size")
        if calls[0] == 2:
            raise PermissionError("the file is being replaced")
        return real(source, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pl, "read_parquet", torn)
    monkeypatch.setattr("tbot.data.store.time.sleep", lambda seconds: None)
    assert store.read("BTCUSDT", H4).height == 3
    assert calls[0] == 3


def test_a_damaged_file_set_aside_is_kept_and_its_year_marked(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    store.write("BTCUSDT", H4, make_bars(NEW_YEAR_EVE, 12, H4))
    directory = store.directory("BTCUSDT", H4)
    for copy in ("2023.parquet.damaged", "2023.parquet.damaged2"):
        (directory / "2023.parquet").write_bytes(b"PAR1garbagePAR1")
        assert store.set_aside(directory / "2023.parquet") == directory / copy
        assert (directory / copy).read_bytes() == b"PAR1garbagePAR1"
    assert store.years_to_refetch("BTCUSDT", H4) == [2023]
    assert store.read("BTCUSDT", H4)["open_time"].min() == datetime(2024, 1, 1, tzinfo=UTC)
    # Another process mended it before this one got the lock: it stays.
    assert store.set_aside(directory / "2024.parquet") is None
    assert store.read("BTCUSDT", H4).height == 6
    store.refetched("BTCUSDT", H4, 2023)
    assert store.years_to_refetch("BTCUSDT", H4) == []
