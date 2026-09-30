from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from binance_fake import FakeBinance, make_rows
from tbot.core.timeframe import Timeframe
from tbot.data import binance_rest
from tbot.data.downloader import iter_months, sync
from tbot.data.store import BarStore

H1 = Timeframe.H1
JAN_1 = datetime(2024, 1, 1, tzinfo=UTC)
# Jan 744 + Feb 696 + Mar 1-14 336 + Mar 15 00:00-10:00 11; the 10:00 bar is still open.
ROWS = make_rows(JAN_1, 1787, H1)
NOW = datetime(2024, 3, 15, 10, 30, tzinfo=UTC)
LAST_CLOSED = datetime(2024, 3, 15, 9, tzinfo=UTC)


@pytest.mark.parametrize(
    ("first", "stop", "expected"),
    [
        (date(2023, 11, 5), date(2024, 2, 1), [(2023, 11), (2023, 12), (2024, 1)]),
        (date(2024, 3, 1), date(2024, 3, 31), []),
    ],
)
def test_iter_months(first: date, stop: date, expected: list[tuple[int, int]]) -> None:
    assert list(iter_months(first, stop)) == expected


def test_fresh_sync_uses_archives_then_rest(tmp_path: Path) -> None:
    fake = FakeBinance("BTCUSDT", H1, ROWS, micros=True)
    store = BarStore(tmp_path)
    start = datetime(2023, 11, 1, tzinfo=UTC)  # before listing: those archives are missing
    with fake.client() as client:
        result = sync(store, client, "BTCUSDT", H1, start, NOW)

    assert (result.archive_bars, result.rest_bars, result.total_bars) == (1440, 346, 1786)
    assert (result.first, result.last) == (JAN_1, LAST_CLOSED)
    assert store.read("BTCUSDT", H1)["open_time"].is_unique().all()


def test_unpublished_month_falls_back_to_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(binance_rest, "MAX_LIMIT", 100)  # force pagination
    fake = FakeBinance("BTCUSDT", H1, ROWS, unpublished={(2024, 2)})
    with fake.client() as client:
        result = sync(BarStore(tmp_path), client, "BTCUSDT", H1, JAN_1, NOW)

    assert (result.archive_bars, result.rest_bars, result.total_bars) == (744, 1042, 1786)
    assert result.last == LAST_CLOSED


def test_incremental_sync_only_fetches_new_months(tmp_path: Path) -> None:
    fake = FakeBinance("BTCUSDT", H1, ROWS)
    store = BarStore(tmp_path)
    with fake.client() as client:
        first = sync(store, client, "BTCUSDT", H1, JAN_1, datetime(2024, 2, 10, 0, 30, tzinfo=UTC))
        fake.requests.clear()
        second = sync(store, client, "BTCUSDT", H1, JAN_1, NOW)

    assert first.total_bars == 744 + 9 * 24
    assert [url.rsplit("/", 1)[-1] for url in fake.archive_requests()] == ["BTCUSDT-1h-2024-02.zip"]
    assert (second.total_bars, second.last) == (1786, LAST_CLOSED)


def test_misaligned_bars_are_dropped(tmp_path: Path) -> None:
    rows = make_rows(JAN_1, 48, H1)
    rows[10][0] += 28 * 60 * 1000  # off-grid bar, as after the Feb 2018 outage
    fake = FakeBinance("BTCUSDT", H1, rows)
    store = BarStore(tmp_path)
    with fake.client() as client:
        result = sync(store, client, "BTCUSDT", H1, JAN_1, datetime(2024, 2, 2, tzinfo=UTC))

    assert (result.archive_bars, result.dropped, result.total_bars) == (47, 1, 47)
    assert datetime(2024, 1, 1, 10, tzinfo=UTC) not in store.read("BTCUSDT", H1)["open_time"]


def test_start_filters_earlier_bars(tmp_path: Path) -> None:
    fake = FakeBinance("BTCUSDT", H1, ROWS)
    start = datetime(2024, 1, 20, tzinfo=UTC)
    with fake.client() as client:
        result = sync(BarStore(tmp_path), client, "BTCUSDT", H1, start, NOW)
    assert result.first == start
    assert result.total_bars == 1786 - 19 * 24


def test_unpublished_month_between_archives_comes_from_rest(tmp_path: Path) -> None:
    rows = make_rows(JAN_1, 24 * 91 + 11, H1)  # through Apr 1 10:00; that bar is still open
    now = datetime(2024, 4, 1, 10, 30, tzinfo=UTC)
    fake = FakeBinance("BTCUSDT", H1, rows, unpublished={(2024, 2)})
    with fake.client() as client:
        result = sync(BarStore(tmp_path), client, "BTCUSDT", H1, JAN_1, now)

    assert (result.archive_bars, result.rest_bars) == (744 + 744, 696 + 10)
    assert (result.total_bars, result.last) == (24 * 91 + 10, datetime(2024, 4, 1, 9, tzinfo=UTC))


def test_later_start_does_not_skip_bars_after_the_last_stored(tmp_path: Path) -> None:
    fake = FakeBinance("BTCUSDT", H1, ROWS)
    store = BarStore(tmp_path)
    with fake.client() as client:
        sync(store, client, "BTCUSDT", H1, JAN_1, datetime(2024, 1, 10, 0, 30, tzinfo=UTC))
        result = sync(store, client, "BTCUSDT", H1, datetime(2024, 3, 1, tzinfo=UTC), NOW)

    assert (result.total_bars, result.last) == (1786, LAST_CLOSED)
    steps = store.read("BTCUSDT", H1)["open_time"].diff().drop_nulls().unique().to_list()
    assert steps == [timedelta(hours=1)]
