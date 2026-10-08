import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import polars as pl
import pytest

from binance_fake import Row, make_rows
from tbot.core.timeframe import Timeframe, to_millis
from tbot.data.binance_rest import fetch_klines
from tbot.data.schema import from_rows
from tbot.data.store import BarStore
from tbot.live.clock import CLOSE_GRACE
from tbot.live.feed import LiveFeed, StreamKey, close_time

H1, H4 = Timeframe.H1, Timeframe.H4
T0 = datetime(2024, 1, 1, tzinfo=UTC)
BTC1 = ("BTCUSDT", H1)


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def rest_client(rows_by_key: dict[StreamKey, list[Row]]) -> tuple[httpx.Client, list[str]]:
    """Serve /api/v3/klines from in-memory rows and record the requests."""
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        params = request.url.params
        key = (params["symbol"], Timeframe(params["interval"]))
        start, end = int(params["startTime"]), int(params["endTime"])
        rows = [r for r in rows_by_key.get(key, []) if start <= r[0] <= end]
        return httpx.Response(200, json=rows[: int(params["limit"])])

    return httpx.Client(transport=httpx.MockTransport(handle)), requests


def kline_message(key: StreamKey, open_time: datetime, closed: bool, price: float = 100.0) -> str:
    symbol, timeframe = key
    open_ms = to_millis(open_time)
    p = f"{price:.2f}"
    k = {
        "t": open_ms,
        "T": open_ms + timeframe.millis - 1,
        "s": symbol,
        "i": str(timeframe),
        "o": p,
        "h": p,
        "l": p,
        "c": p,
        "v": "1.0",
        "n": 3,
        "x": closed,
        "q": "100.0",
        "V": "0.5",
        "Q": "50.0",
    }
    return json.dumps(
        {
            "stream": f"{symbol.lower()}@kline_{timeframe}",
            "data": {"e": "kline", "s": symbol, "k": k},
        }
    )


Messages = list[str | Exception]


def fake_connector(connections: list[Messages]) -> tuple[Callable[..., object], list[str]]:
    """Each connection replays its messages; an Exception item is raised; the last hangs."""
    urls: list[str] = []

    @asynccontextmanager
    async def connect(url: str) -> AsyncIterator[AsyncIterator[str | bytes]]:
        urls.append(url)
        messages = connections[min(len(urls) - 1, len(connections) - 1)]

        async def stream() -> AsyncIterator[str | bytes]:
            for item in messages:
                if isinstance(item, Exception):
                    raise item
                yield item
            await asyncio.Event().wait()  # stay connected

        yield stream()

    return connect, urls


def make_feed(
    tmp_path: Path,
    keys: list[StreamKey],
    now: datetime,
    rows: dict[StreamKey, list[Row]] | None = None,
    **options: object,
) -> tuple[LiveFeed, BarStore, list[str]]:
    store = BarStore(tmp_path / "data")
    client, requests = rest_client(rows or {})
    feed = LiveFeed(keys, store, client, clock=lambda: now, **options)  # type: ignore[arg-type]
    return feed, store, requests


def test_emit_stores_new_closed_bars_once(tmp_path: Path) -> None:
    feed, store, _ = make_feed(tmp_path, [BTC1], now=at(4.5))
    bars = from_rows(make_rows(T0, 6, H1))
    assert feed._emit(BTC1, bars) == 4  # opens 0h..3h are closed at 4:30; 4h and 5h are not
    assert feed.queue.qsize() == 4
    assert store.last_open_time(*BTC1) == at(3)
    assert feed.last[BTC1] == at(3)
    assert feed._emit(BTC1, bars) == 0
    assert feed._emit(BTC1, bars, exchange_closed=True) == 2  # exchange says closed: trusted


def test_catch_up_fetches_missing_bars_via_rest(tmp_path: Path) -> None:
    rows = {BTC1: make_rows(T0, 10, H1)}
    feed, store, requests = make_feed(tmp_path, [BTC1], now=at(8.2), rows=rows)
    store.write(*BTC1, from_rows(rows[BTC1][:3]))
    feed.last[BTC1] = store.last_open_time(*BTC1)

    assert asyncio.run(feed.catch_up()) == 5  # opens 3h..7h
    assert len(requests) == 1
    assert feed.last[BTC1] == at(7)
    assert asyncio.run(feed.catch_up()) == 0


def test_websocket_emits_closed_bars_dedups_and_reconnects(tmp_path: Path) -> None:
    rows = {BTC1: make_rows(T0, 4, H1)}  # REST knows bars up to open 3h
    # Local clock 5 s behind the exchange: the socket's closed flag must win over the clock.
    feed, store, _ = make_feed(tmp_path, [BTC1], now=at(5) - timedelta(seconds=5), rows=rows)
    store.write(*BTC1, from_rows(rows[BTC1][:3]))
    feed.last[BTC1] = at(2)
    connect, urls = fake_connector(
        [
            [
                kline_message(BTC1, at(4), closed=False),  # ignored
                kline_message(BTC1, at(3), closed=True),  # already emitted by catch-up
                RuntimeError("connection dropped"),
            ],
            [kline_message(BTC1, at(4), closed=True, price=123.0)],
        ]
    )
    feed.connector = connect  # type: ignore[assignment]

    async def run() -> list[tuple[StreamKey, pl.DataFrame]]:
        task = asyncio.create_task(feed.run_websocket())
        items = [await asyncio.wait_for(feed.queue.get(), 5) for _ in range(2)]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return items

    items = asyncio.run(run())
    assert [bar["open_time"][0] for _, bar in items] == [at(3), at(4)]
    assert items[1][1]["close"][0] == 123.0
    assert feed.reconnects == 1
    assert len(urls) == 2
    assert "btcusdt@kline_1h" in urls[0]
    assert store.last_open_time(*BTC1) == at(4)


def test_batches_group_streams_closing_together(tmp_path: Path) -> None:
    eth1, btc4 = ("ETHUSDT", H1), ("BTCUSDT", H4)
    feed, _, _ = make_feed(tmp_path, [BTC1, eth1, btc4], now=at(6), batch_wait=0.05)
    one = from_rows(make_rows(at(3), 1, H1))
    feed.queue.put_nowait((BTC1, one))
    feed.queue.put_nowait((eth1, one))
    feed.queue.put_nowait((btc4, from_rows(make_rows(T0, 1, H4))))
    feed.queue.put_nowait((BTC1, from_rows(make_rows(at(4), 1, H1))))

    async def take(n: int) -> list[set[StreamKey]]:
        batches = feed.batches()
        return [set(await anext(batches)) for _ in range(n)]

    assert asyncio.run(take(2)) == [{BTC1, eth1, btc4}, {BTC1}]


def test_batches_keep_each_stream_in_order_after_catch_up(tmp_path: Path) -> None:
    eth1 = ("ETHUSDT", H1)
    feed, _, _ = make_feed(tmp_path, [BTC1, eth1], now=at(6), batch_wait=0.05)
    for key in (BTC1, eth1):  # catch-up queues all bars of one stream, then the next stream
        for i in range(3):
            feed.queue.put_nowait((key, from_rows(make_rows(at(i), 1, H1))))

    async def take(n: int) -> list[dict[StreamKey, datetime]]:
        batches = feed.batches()
        return [
            {key: bar["open_time"][0] for key, bar in (await anext(batches)).items()}
            for _ in range(n)
        ]

    assert asyncio.run(take(3)) == [{BTC1: at(i), eth1: at(i)} for i in range(3)]


def test_websocket_fills_a_gap_via_rest_before_emitting(tmp_path: Path) -> None:
    rows = {BTC1: make_rows(T0, 6, H1)}
    store = BarStore(tmp_path / "data")
    store.write(*BTC1, from_rows(rows[BTC1][:2]))  # opens 0h and 1h
    client, _ = rest_client(rows)
    clock = {"now": at(2.5)}  # nothing to catch up when the socket connects
    feed = LiveFeed([BTC1], store, client, clock=lambda: clock["now"])

    @asynccontextmanager
    async def connect(url: str) -> AsyncIterator[AsyncIterator[str | bytes]]:
        async def stream() -> AsyncIterator[str | bytes]:
            clock["now"] = at(5.1)  # the 2h and 3h closes were missed while connected
            yield kline_message(BTC1, at(4), closed=True)
            await asyncio.Event().wait()

        yield stream()

    feed.connector = connect

    async def run() -> list[datetime]:
        task = asyncio.create_task(feed.run_websocket())
        opens = []
        for _ in range(3):
            _, bar = await asyncio.wait_for(feed.queue.get(), 5)
            opens.append(bar["open_time"][0])
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return opens

    assert asyncio.run(run()) == [at(2), at(3), at(4)]
    assert feed.queue.empty()  # the socket's own copy of the 4h bar is not emitted again
    assert store.last_open_time(*BTC1) == at(4)


def test_watchdog_survives_a_rest_failure(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"Retry-After": "0"})

    client = httpx.Client(transport=httpx.MockTransport(handle))
    feed = LiveFeed(
        [BTC1], BarStore(tmp_path / "data"), client, clock=lambda: at(5.5), poll_seconds=0.01
    )
    feed.last[BTC1] = at(2)

    async def run() -> bool:
        task = asyncio.create_task(feed.run_watchdog())
        await asyncio.sleep(0.15)
        alive = not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return alive

    assert asyncio.run(run())


def test_overdue_seconds(tmp_path: Path) -> None:
    feed, _, _ = make_feed(tmp_path, [BTC1], now=at(5.25))
    grace = CLOSE_GRACE.total_seconds()  # a bar counts as closed only after it
    assert feed._overdue_seconds(BTC1, at(5.25)) == pytest.approx(15 * 60 - grace)
    feed.last[BTC1] = at(4)
    assert feed._overdue_seconds(BTC1, at(5.25)) == 0.0
    feed.last[BTC1] = at(2)  # the 3h bar was due at 4h
    assert feed._overdue_seconds(BTC1, at(5.25)) == pytest.approx(75 * 60 - grace)


def test_watchdog_polls_rest_and_reports_stale_once(tmp_path: Path) -> None:
    stale: list[StreamKey] = []
    rows = {BTC1: make_rows(T0, 3, H1)}  # REST has nothing newer than open 2h
    feed, store, requests = make_feed(
        tmp_path,
        [BTC1],
        now=at(5.5),
        rows=rows,
        poll_seconds=0.01,
        stale_after=60,
        on_stale=lambda key, last: stale.append(key),
    )
    store.write(*BTC1, from_rows(rows[BTC1]))
    feed.last[BTC1] = at(2)

    async def run() -> None:
        task = asyncio.create_task(feed.run_watchdog())
        await asyncio.sleep(0.2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert len(requests) >= 2
    assert stale == [BTC1]


def test_server_clock_measures_offset() -> None:
    import time

    from tbot.live.clock import ServerClock

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"serverTime": int((time.time() + 5) * 1000)})

    clock = ServerClock(httpx.Client(transport=httpx.MockTransport(handle)))
    assert clock.now() == pytest.approx(datetime.now(UTC), abs=timedelta(seconds=1))
    assert clock.sync() == pytest.approx(5.0, abs=0.5)
    assert clock.now() - datetime.now(UTC) == pytest.approx(
        timedelta(seconds=5), abs=timedelta(seconds=0.5)
    )


def test_server_clock_ignores_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    from tbot.live.clock import ServerClock

    calls = [0]

    def handle(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        if calls[0] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"serverTime": int((time.time() + 5) * 1000)})

    monkeypatch.setattr("tbot.live.clock.time.sleep", lambda seconds: None)
    clock = ServerClock(httpx.Client(transport=httpx.MockTransport(handle)))
    assert clock.sync() == pytest.approx(5.0, abs=0.3)
    assert calls[0] == 2


def test_feed_starts_from_what_the_session_has_seen(tmp_path: Path) -> None:
    store = BarStore(tmp_path / "data")
    store.write(*BTC1, from_rows(make_rows(T0, 4, H1)))  # another process stored a newer bar
    client, _ = rest_client({})
    feed = LiveFeed([BTC1], store, client, clock=lambda: at(4.5), last={BTC1: at(2)})
    assert feed.last[BTC1] == at(2)
    assert LiveFeed([BTC1], store, client, clock=lambda: at(4.5)).last[BTC1] == at(3)


def test_catch_up_queues_bars_in_close_order(tmp_path: Path) -> None:
    btc4 = ("BTCUSDT", H4)
    rows = {BTC1: make_rows(T0, 9, H1), btc4: make_rows(T0, 2, H4)}
    feed, _, _ = make_feed(tmp_path, [BTC1, btc4], now=at(8.5), rows=rows)
    feed.last = {BTC1: T0 - H1.delta, btc4: T0 - H4.delta}
    assert asyncio.run(feed.catch_up()) == 8 + 2
    closes = []
    while not feed.queue.empty():
        key, bar = feed.queue.get_nowait()
        closes.append((close_time(key, bar), key[1]))
    assert closes == sorted(closes, key=lambda item: (item[0], item[1].millis))
    assert closes[3:5] == [(at(4), H1), (at(4), H4)]  # together, before the 5h close


def test_a_failed_catch_up_loses_no_bar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eth = ("ETHUSDT", H1)
    feed, _, _ = make_feed(tmp_path, [BTC1, eth], now=at(5.5), rows={BTC1: make_rows(T0, 6, H1)})
    feed.last = {BTC1: at(1), eth: at(1)}

    def broken(client: httpx.Client, symbol: str, *args: object) -> pl.DataFrame:
        if symbol == "ETHUSDT":
            raise httpx.ConnectError("down")
        return fetch_klines(client, symbol, *args)  # type: ignore[arg-type]

    monkeypatch.setattr("tbot.live.feed.fetch_klines", broken)
    with pytest.raises(httpx.ConnectError):
        asyncio.run(feed.catch_up())
    assert feed.last[BTC1] == at(1)  # nothing was taken in, so the retry gets it all
    assert feed.queue.empty()
    assert asyncio.run(feed.catch_up([BTC1])) == 3


def test_a_bar_just_closed_by_the_clock_waits_for_the_grace(tmp_path: Path) -> None:
    rows = {BTC1: make_rows(at(0), 6, H1)}  # opens 0h..5h; the clock may run a little fast
    feed, store, _ = make_feed(tmp_path, [BTC1], now=at(5) + timedelta(seconds=2), rows=rows)
    feed.last[BTC1] = at(2)
    # The 4h bar closed 2 s ago by a clock that may run ahead: only the 3h bar is taken.
    assert asyncio.run(feed.catch_up()) == 1
    assert store.last_open_time(*BTC1) == at(3)


def test_server_clock_keeps_its_offset_when_every_sample_is_slow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from tbot.live.clock import ServerClock

    now = [1000.0]
    step = [0.1]  # seconds each request takes
    skew = [5.0]

    def fake_time() -> float:
        now[0] += step[0] / 2  # called once before and once after each request
        return now[0]

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"serverTime": int((now[0] + skew[0]) * 1000)})

    monkeypatch.setattr("tbot.live.clock.time", SimpleNamespace(time=fake_time, sleep=lambda s: 0))
    clock = ServerClock(httpx.Client(transport=httpx.MockTransport(handle)))
    assert clock.sync() == pytest.approx(5.0, abs=0.1)
    step[0], skew[0] = 6.0, 9.0  # a congested network: no sample is worth trusting
    assert clock.sync() == pytest.approx(5.0, abs=0.1)
    step[0] = 0.1
    assert clock.sync() == pytest.approx(9.0, abs=0.1)


def test_a_rate_limit_keeps_the_feed_away_from_rest(tmp_path: Path) -> None:
    calls = [0]

    def handle(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        return httpx.Response(429, headers={"Retry-After": "120"})

    store = BarStore(tmp_path / "data")
    now = [at(5.5)]
    feed = LiveFeed(
        [BTC1],
        store,
        httpx.Client(transport=httpx.MockTransport(handle)),
        clock=lambda: now[0],
        last={BTC1: at(2)},
    )
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(feed.catch_up())
    assert calls == [1]
    assert asyncio.run(feed.catch_up()) == 0  # within the wait: no request at all
    assert calls == [1]
    now[0] += timedelta(seconds=121)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(feed.catch_up())
    assert calls == [2]
