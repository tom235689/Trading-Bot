from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from binance_fake import FakeBinance, make_rows
from tbot.core.timeframe import Timeframe
from tbot.data import binance_rest, downloader
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


def test_earlier_start_backfills_before_the_first_stored_bar(tmp_path: Path) -> None:
    fake = FakeBinance("BTCUSDT", H1, ROWS)
    store = BarStore(tmp_path)
    with fake.client() as client:
        sync(store, client, "BTCUSDT", H1, datetime(2024, 2, 10, tzinfo=UTC), NOW)
        fake.requests.clear()
        result = sync(store, client, "BTCUSDT", H1, JAN_1, NOW)

    assert (result.total_bars, result.first, result.last) == (1786, JAN_1, LAST_CLOSED)
    assert (result.archive_bars, result.rest_bars) == (744, 9 * 24)  # Jan, then Feb 1-9
    assert [url.rsplit("/", 1)[-1] for url in fake.archive_requests()] == ["BTCUSDT-1h-2024-01.zip"]
    steps = store.read("BTCUSDT", H1)["open_time"].diff().drop_nulls().unique().to_list()
    assert steps == [timedelta(hours=1)]


def test_start_before_listing_costs_one_probe_per_sync(tmp_path: Path) -> None:
    fake = FakeBinance("BTCUSDT", H1, ROWS)
    store = BarStore(tmp_path)
    before_listing = datetime(2023, 6, 1, tzinfo=UTC)
    with fake.client() as client:
        sync(store, client, "BTCUSDT", H1, before_listing, NOW)
        fake.requests.clear()
        result = sync(store, client, "BTCUSDT", H1, before_listing, NOW)

    assert (result.total_bars, result.first) == (1786, JAN_1)
    assert fake.archive_requests() == []
    assert len(fake.requests) == 1


def test_failed_backfill_leaves_the_store_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBinance("BTCUSDT", H1, ROWS)
    store = BarStore(tmp_path)
    with fake.client() as client:
        sync(store, client, "BTCUSDT", H1, datetime(2024, 2, 10, tzinfo=UTC), NOW)

        def down(*args: object) -> object:
            raise httpx.ConnectError("down")

        monkeypatch.setattr(downloader, "fetch_klines", down)  # after the January archive
        with pytest.raises(httpx.ConnectError):
            sync(store, client, "BTCUSDT", H1, JAN_1, NOW)
    assert store.first_open_time("BTCUSDT", H1) == datetime(2024, 2, 10, tzinfo=UTC)


def _gaps(store: BarStore, timeframe: Timeframe) -> int:
    from tbot.data.quality import check_bars

    bars = store.read("BTCUSDT", timeframe)
    return len(check_bars(bars, timeframe, datetime(2030, 1, 1, tzinfo=UTC)).gaps)


H4 = Timeframe.H4
LONG = make_rows(datetime(2023, 1, 1, tzinfo=UTC), 6 * (365 + 366 + 60), H4)
LATER = datetime(2025, 3, 1, 12, tzinfo=UTC)


def test_a_backfill_that_fails_halfway_leaves_no_hole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tbot.data import store as store_module

    fake = FakeBinance("BTCUSDT", H4, LONG)
    store = BarStore(tmp_path)
    with fake.client() as client:
        sync(store, client, "BTCUSDT", H4, datetime(2025, 2, 1, tzinfo=UTC), LATER)
        real = store_module._replace

        def busy(source: Path, target: Path) -> None:
            if target.name == "2025.parquet":  # another process has it open
                source.unlink()
                raise PermissionError("in use")
            real(source, target)

        monkeypatch.setattr(store_module, "_replace", busy)
        with pytest.raises(PermissionError):
            sync(store, client, "BTCUSDT", H4, datetime(2023, 1, 1, tzinfo=UTC), LATER)
        assert _gaps(store, H4) == 0  # the year next to the stored bars goes in first
        monkeypatch.setattr(store_module, "_replace", real)
        sync(store, client, "BTCUSDT", H4, datetime(2023, 1, 1, tzinfo=UTC), LATER)
    assert _gaps(store, H4) == 0
    assert store.first_open_time("BTCUSDT", H4) == datetime(2023, 1, 1, tzinfo=UTC)


def test_a_month_missing_from_the_archives_is_filled_before_later_ones_are_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBinance("BTCUSDT", H4, LONG, unpublished={(2023, 6)})
    store = BarStore(tmp_path)

    def down(*args: object, **kwargs: object) -> object:
        raise httpx.ConnectError("down")

    with fake.client() as client:
        monkeypatch.setattr(downloader, "fetch_klines", down)
        with pytest.raises(httpx.ConnectError):
            sync(store, client, "BTCUSDT", H4, datetime(2023, 1, 1, tzinfo=UTC), LATER)
        assert _gaps(store, H4) == 0  # nothing after the missing month was stored
        monkeypatch.undo()  # REST works again
        sync(store, client, "BTCUSDT", H4, datetime(2023, 1, 1, tzinfo=UTC), LATER)
    assert _gaps(store, H4) == 0


def test_a_delisted_symbol_keeps_its_archives(tmp_path: Path) -> None:
    class Delisted(FakeBinance):
        def _klines(self, params: object) -> httpx.Response:
            return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})

    fake = Delisted("BTCUSDT", H4, make_rows(datetime(2023, 1, 1, tzinfo=UTC), 6 * 200, H4))
    store = BarStore(tmp_path)
    with fake.client() as client:
        for _ in range(2):  # the second sync must not fail either
            result = sync(store, client, "BTCUSDT", H4, datetime(2023, 1, 1, tzinfo=UTC), LATER)
    assert result.rest_bars == 0
    assert result.last == datetime(2023, 7, 19, 20, tzinfo=UTC)  # the last archived bar


class Delisted(FakeBinance):
    def _klines(self, params: object) -> httpx.Response:
        return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})


def test_a_delisted_symbol_backfills_a_partial_month_from_its_archive(tmp_path: Path) -> None:
    fake = Delisted("BTCUSDT", H4, make_rows(datetime(2023, 1, 1, tzinfo=UTC), 6 * 200, H4))
    store = BarStore(tmp_path)
    with fake.client() as client:
        sync(store, client, "BTCUSDT", H4, datetime(2023, 3, 15, tzinfo=UTC), LATER)
        sync(store, client, "BTCUSDT", H4, datetime(2023, 1, 1, tzinfo=UTC), LATER)
    assert store.read("BTCUSDT", H4).height == 6 * 200  # March 1-14 too
    assert _gaps(store, H4) == 0


def test_a_symbol_that_never_existed_is_an_error(tmp_path: Path) -> None:
    fake = Delisted("BTCUSDT", H4, [])
    with fake.client() as client, pytest.raises(LookupError, match="does not list it"):
        sync(BarStore(tmp_path), client, "BTCUSDT", H4, datetime(2024, 1, 1, tzinfo=UTC), LATER)


def test_another_rejection_is_not_taken_for_a_delisting(tmp_path: Path) -> None:
    class Rejecting(FakeBinance):
        def _klines(self, params: object) -> httpx.Response:
            return httpx.Response(400, json={"code": -1100, "msg": "Illegal characters."})

    fake = Rejecting("BTCUSDT", H4, make_rows(datetime(2023, 1, 1, tzinfo=UTC), 6 * 200, H4))
    with fake.client() as client, pytest.raises(httpx.HTTPStatusError):
        sync(BarStore(tmp_path), client, "BTCUSDT", H4, datetime(2023, 1, 1, tzinfo=UTC), LATER)


def test_months_before_the_listing_do_not_hold_back_the_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed = datetime(2023, 3, 10, tzinfo=UTC)
    fake = FakeBinance("BTCUSDT", H4, make_rows(listed, 6 * 700, H4))
    store = BarStore(tmp_path)
    real = binance_rest.fetch_klines

    def tail_fails(client: httpx.Client, *args: object) -> object:
        if args[2] >= datetime(2025, 1, 1, tzinfo=UTC):  # type: ignore[operator]
            raise httpx.ConnectError("blip")
        return real(client, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(downloader, "fetch_klines", tail_fails)
    with fake.client() as client, pytest.raises(httpx.ConnectError):
        sync(store, client, "BTCUSDT", H4, datetime(2023, 1, 1, tzinfo=UTC), LATER)
    assert store.read("BTCUSDT", H4).height > 4000  # the archives were stored as they came
    assert _gaps(store, H4) == 0
