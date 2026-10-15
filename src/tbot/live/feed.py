"""Live feed of closed bars: WebSocket first, REST catch-up for anything missed.

Contract: every closed bar of every stream is emitted exactly once, in order,
after being written to the bar store. The store is the memory of what was seen.
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx
import polars as pl
import structlog

from tbot.core.timeframe import Timeframe
from tbot.data import binance_ws
from tbot.data.binance_rest import fetch_klines
from tbot.data.http import retry_after
from tbot.data.quality import aligned
from tbot.data.store import BarStore, StoreUnreadable
from tbot.live.clock import CLOSE_GRACE

StreamKey = tuple[str, Timeframe]
Connector = Callable[[str], AbstractAsyncContextManager[AsyncIterator[str | bytes]]]
Batch = dict[StreamKey, pl.DataFrame]

log = structlog.get_logger(__name__)
FETCH_ERRORS = (httpx.HTTPError, LookupError, ValueError, OSError)  # REST failures, retried
RETRY_SECONDS = 1.0  # the first reconnect waits this long, each next one twice as long
HEALTHY_SECONDS = 60.0  # a socket open this long resets the reconnect backoff
STORE_WAIT_SECONDS = 5.0  # for another process's merge into a stream, which takes under 1 s
# Binance pushes a kline stream with every trade, about every 2 s on busy pairs. A socket
# silent this long across a bar close has stalled while still answering pings, and is
# replaced; quiet pairs may send nothing for minutes between closes.
SILENCE_SECONDS = 120.0
DOWN_ALERT_SECONDS = 900.0  # no socket for this long is alerted once; REST still brings bars


def utc_now() -> datetime:
    return datetime.now(UTC)


_Item = tuple[int, StreamKey, pl.DataFrame]  # close time, stream, one bar


def _item(key: StreamKey, bar: pl.DataFrame) -> _Item:
    return int(bar["open_time"].dt.epoch("ms")[0]) + key[1].millis, key, bar


class LiveFeed:
    def __init__(
        self,
        keys: Sequence[StreamKey],
        store: BarStore,
        client: httpx.Client,
        *,
        connector: Connector = binance_ws.connect,
        clock: Callable[[], datetime] = utc_now,
        stale_after: float = 600.0,
        poll_seconds: float = 30.0,
        batch_wait: float = 5.0,
        on_stale: Callable[[StreamKey, datetime], None] | None = None,
        on_fresh: Callable[[StreamKey], None] | None = None,
        on_alert: Callable[[str], None] | None = None,
        last: Mapping[StreamKey, datetime | None] | None = None,
    ) -> None:
        self.keys = list(keys)
        self.store = store
        self.client = client
        self.connector = connector
        self.clock = clock
        self.stale_after = stale_after
        self.poll_seconds = poll_seconds
        self.batch_wait = batch_wait
        self.on_stale = on_stale
        self.on_fresh = on_fresh  # a stream reported stale has bars again
        self.on_alert = on_alert  # a problem of the feed for the owner, in one line
        self.last: dict[StreamKey, datetime | None] = {
            key: last[key] if last is not None else store.last_open_time(*key) for key in self.keys
        }
        self.queue: asyncio.Queue[tuple[StreamKey, pl.DataFrame]] = asyncio.Queue()
        self.reconnects = 0
        self.silence = SILENCE_SECONDS
        self.endpoints = binance_ws.STREAM_URLS
        self.down_alert_after = DOWN_ALERT_SECONDS
        self._stale_reported: set[StreamKey] = set()
        self._unreadable: set[StreamKey] = set()  # a year file the store cannot read
        self.rest_paused_until: datetime | None = None  # a rate limit: calling now risks a ban
        # Held while bars are stored and queued: a store runs in a thread, and another
        # source queueing bars meanwhile could split an event or repeat a bar.
        self._storing = asyncio.Lock()

    # emission

    async def _emit(
        self, key: StreamKey, bars: pl.DataFrame, *, exchange_closed: bool = False
    ) -> int:
        """Store and queue bars that are new, closed, and on the grid. Return the count."""
        async with self._storing:
            accepted = await self._accept(key, bars, exchange_closed=exchange_closed)
            for row in accepted.iter_slices(1):
                self.queue.put_nowait((key, row))
        return accepted.height

    async def _accept(
        self, key: StreamKey, bars: pl.DataFrame, *, exchange_closed: bool = False
    ) -> pl.DataFrame:
        """Store the bars that are new, closed, and on the grid, and return them. The
        caller holds _storing.

        The exchange marks socket bars closed itself; REST bars are judged by the clock,
        which must be server-synced or a fast local clock stores a still-open bar.
        """
        timeframe = key[1]
        bars = bars.filter(aligned(timeframe))
        if not exchange_closed:
            closed_by = timeframe.floor(self.clock() - CLOSE_GRACE)
            bars = bars.filter(pl.col("open_time") < closed_by)
        last = self.last[key]
        if last is not None:
            bars = bars.filter(pl.col("open_time") > last)
        if bars.is_empty():
            return bars
        bars = bars.sort("open_time")
        # In a thread: a merge rewrites the year file, a quarter second for 1m bars. A long
        # wait for another writer would still hold up the trader; StoreBusy leaves the bars
        # to the next catch-up.
        try:
            await asyncio.to_thread(self.store.write, *key, bars, wait=STORE_WAIT_SECONDS)
        except StoreUnreadable as exc:
            self._unreadable_stream(key, exc)
            raise
        self._unreadable.discard(key)
        self.last[key] = bars["open_time"][-1]
        if key in self._stale_reported:
            self._stale_reported.discard(key)
            if self.on_fresh is not None:
                _report(self.on_fresh, key)  # the bars are stored: they must still be queued
        return bars

    def _unreadable_stream(self, key: StreamKey, exc: StoreUnreadable) -> None:
        """A stream whose year file cannot be read gets no bars until the file is fixed:
        it counts as stale, and the file and the cure are said once."""
        if key in self._unreadable:
            return
        self._unreadable.add(key)
        self._stale_reported.add(key)
        log.error("store_unreadable", **_fields(key), error=str(exc))
        self._alert(f"stream {_name(key)} gets no bars: {exc}")

    def _alert(self, text: str) -> None:
        if self.on_alert is not None:
            _report(self.on_alert, text)

    def stale(self) -> list[StreamKey]:
        """Streams reported stale that have not delivered a bar since."""
        return [key for key in self.keys if key in self._stale_reported]

    async def catch_up(self, keys: Sequence[StreamKey] | None = None) -> int:
        """Fetch bars closed since the last emitted one via REST.

        Every stream is fetched before any bar is stored or queued, then they are queued
        in close order at once, and nothing else is stored or queued in between: a 1h bar
        queued ahead of the 4h bar closing with it would release that event without it.
        A stream whose fetch fails stays behind and is asked again later; the others go
        on, and the first failure is raised once they are queued. So does a stream the
        store cannot take.
        """
        if self._rest_paused():
            return 0  # the watchdog asks again; stale streams are still reported
        fetched = []
        failure: Exception | None = None
        for key in keys or self.keys:
            symbol, timeframe = key
            last = self.last[key]
            end = timeframe.floor(self.clock() - CLOSE_GRACE)
            start = last + timeframe.delta if last is not None else end - timeframe.delta * 2
            if start < end:
                try:
                    bars = await asyncio.to_thread(
                        fetch_klines, self.client, symbol, timeframe, start, end
                    )
                except FETCH_ERRORS as exc:
                    log.warning(
                        "catch_up_failed", symbol=symbol, timeframe=str(timeframe), error=repr(exc)
                    )
                    failure = failure or exc
                    wait = retry_after(exc)
                    if wait is not None:
                        self.rest_paused_until = self.clock() + timedelta(seconds=wait)
                        log.warning("rest_rate_limited", seconds=wait)
                        break  # more requests now could get the IP banned
                    continue
                fetched.append((key, bars))
        items: list[_Item] = []
        async with self._storing:
            try:
                for key, bars in fetched:
                    try:
                        accepted = await self._accept(key, bars)  # stored: must be queued
                    except OSError as exc:  # the store is busy, or cannot read a file
                        if not isinstance(exc, StoreUnreadable):  # that one is said once
                            log.warning("store_failed", **_fields(key), error=repr(exc))
                        failure = failure or exc
                        continue
                    if accepted.height:
                        log.info("catch_up", **_fields(key), bars=accepted.height)
                    items.extend(_item(key, row) for row in accepted.iter_slices(1))
            finally:
                items.sort(key=lambda item: (item[0], item[1][1].millis, item[1][0]))
                for _, key, row in items:
                    self.queue.put_nowait((key, row))
        if failure is not None:
            raise failure
        return len(items)

    def _rest_paused(self) -> bool:
        return self.rest_paused_until is not None and self.clock() < self.rest_paused_until

    async def _catch_up_or_log(self, keys: Sequence[StreamKey] | None = None) -> bool:
        """Catch up; True if every stream was fetched and stored. A stream REST or the
        store cannot serve now is left behind for the watchdog."""
        if self._rest_paused():
            return False
        try:
            await self.catch_up(keys)
        except FETCH_ERRORS as exc:
            if not isinstance(exc, StoreUnreadable):  # said once, when it was first met
                log.warning("catch_up_incomplete", error=repr(exc))
            return False
        return True

    # tasks

    async def run_websocket(self) -> None:
        """Keep a socket open. A connect that fails tries the next endpoint (Binance serves
        the streams on ports 9443 and 443; a firewall may block 9443). No socket for
        down_alert_after seconds is alerted once, and so is the next connect."""
        backoff = RETRY_SECONDS
        loop = asyncio.get_running_loop()
        endpoint = 0  # the one that connected last
        down_since = loop.time()  # no socket since
        alerted = False
        while True:
            base = self.endpoints[endpoint]
            opened = loop.time()
            connected = False
            try:
                async with self.connector(binance_ws.stream_url(self.keys, base)) as messages:
                    connected = True
                    log.info("websocket_connected", streams=len(self.keys), endpoint=_host(base))
                    if alerted:
                        alerted = False
                        self._alert(f"market data socket connected again ({_host(base)})")
                    await self._catch_up_or_log()
                    await self._read(messages)
                error = "closed by the server"  # a clean close: reconnect as after an error
            except asyncio.CancelledError:
                raise
            except TimeoutError:  # a stall, or an opening handshake that never finished
                error = f"no message for {self.silence:.0f} s" if connected else "timed out"
            except Exception as exc:  # any transport failure: reconnect
                error = repr(exc)
            now = loop.time()
            if connected:
                down_since = now
            else:
                error = f"cannot connect to {_host(base)}: {error}"
                endpoint = (endpoint + 1) % len(self.endpoints)
            if now - opened > HEALTHY_SECONDS:
                backoff = RETRY_SECONDS  # it worked for a while: a new failure, not the same one
            self.reconnects += 1
            log.warning("websocket_error", error=error, retry_in=backoff)
            if not connected and not alerted and now - down_since >= self.down_alert_after:
                alerted = True
                tried = " and ".join(_host(url) for url in self.endpoints)
                late = self.poll_seconds + CLOSE_GRACE.total_seconds()
                self._alert(
                    f"no market data socket for {(now - down_since) / 60:.0f} min ({error}; "
                    f"a firewall or proxy may block {tried}): bars still come by REST, up to "
                    f"{late:.0f} s late"
                )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _read(self, messages: AsyncIterator[str | bytes]) -> None:
        """Handle messages until the server closes; raise TimeoutError on a stall."""
        heard = self.clock()
        pending: asyncio.Future[str | bytes] | None = None
        try:
            while True:
                if pending is None:  # kept across a silence: cancelling ends the stream
                    pending = asyncio.ensure_future(anext(messages))
                done, _ = await asyncio.wait({pending}, timeout=self.silence)
                if not done:
                    if self._closed_since(heard):
                        raise TimeoutError
                    continue  # a quiet pair: no trades, no messages
                received, pending = pending, None
                try:
                    message = received.result()
                except StopAsyncIteration:
                    return
                heard = self.clock()
                kline = binance_ws.parse_kline(message)
                if kline is not None and kline.closed and kline.key in self.last:
                    await self._on_socket_bar(kline.key, kline.bar)
        finally:
            if pending is not None:
                pending.cancel()

    def _closed_since(self, heard: datetime) -> bool:
        """Whether a bar closed since the socket last spoke: it sends every close."""
        now = self.clock() - CLOSE_GRACE
        return any(key[1].floor(now) > heard for key in self.keys)

    async def _on_socket_bar(self, key: StreamKey, bar: pl.DataFrame) -> None:
        """Emit a socket bar only if it follows the last one; a gap is filled via REST first.

        Emitting it while REST could still fill the gap would leave a hole in the store
        and the history that no later catch-up could fill, since only bars after the last
        one count. A gap left by a catch-up that fetched everything before the bar is the
        exchange's own (a pause: no bars exist), and the bar is taken at once.
        """
        if self._gap_before(key, bar):
            reach = key[1].floor(self.clock() - CLOSE_GRACE)  # the catch-up fetches up to it
            fetched = await self._catch_up_or_log([key])
            if self._gap_before(key, bar) and not (fetched and reach >= bar["open_time"][0]):
                return  # the watchdog fills the gap, and brings this bar after it
        try:
            await self._emit(key, bar, exchange_closed=True)
        except OSError as exc:  # the store: the watchdog's catch-up brings the bar later
            if not isinstance(exc, StoreUnreadable):  # that one is said once
                log.warning("store_failed", **_fields(key), error=repr(exc))

    def _gap_before(self, key: StreamKey, bar: pl.DataFrame) -> bool:
        last = self.last[key]
        return last is not None and bar["open_time"][0] > last + key[1].delta

    async def run_watchdog(self) -> None:
        """Poll REST for bars the socket did not deliver; report streams that stay stale."""
        while True:
            await asyncio.sleep(self.poll_seconds)
            now = self.clock()
            overdue = [key for key in self.keys if self._overdue_seconds(key, now) > 0]
            if overdue:
                await self._catch_up_or_log(overdue)  # asked again at the next poll
            if self.on_stale is None:
                continue
            for key in self.keys:
                stale = self._overdue_seconds(key, self.clock()) > self.stale_after
                if stale and key not in self._stale_reported:
                    self._stale_reported.add(key)
                    last = self.last[key] or datetime.min.replace(tzinfo=UTC)
                    _report(self.on_stale, key, last)

    def _overdue_seconds(self, key: StreamKey, now: datetime) -> float:
        """Seconds since the bar after the last one should have closed, if it has not come.

        Measured from the first missing close, so a stream that fell several bars
        behind counts as more overdue than one that missed the latest bar only.
        """
        timeframe = key[1]
        last = self.last[key]
        now -= CLOSE_GRACE  # what catch_up can fetch
        due = timeframe.floor(now) if last is None else last + 2 * timeframe.delta
        return max(0.0, (now - due).total_seconds())

    # consumption

    async def batches(self) -> AsyncIterator[Batch]:
        """Group queued bars by close time, earliest close first, so streams closing
        together form one event and every stream stays in order.

        Waits up to batch_wait seconds for the streams expected at that close,
        unless a later bar is already queued, which means the event is over.
        """
        pending: list[_Item] = []
        while True:
            if not pending:
                pending.append(_item(*await self.queue.get()))
            self._drain(pending)
            deadline = asyncio.get_running_loop().time() + self.batch_wait
            while True:
                close_ms = min(item[0] for item in pending)
                if self._ready(pending, close_ms):
                    break
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    pending.append(_item(*await asyncio.wait_for(self.queue.get(), remaining)))
                except TimeoutError:
                    break
                self._drain(pending)
            batch: Batch = {}
            later: list[_Item] = []
            for close, key, bar in pending:
                if close != close_ms:
                    later.append((close, key, bar))
                elif key not in batch:
                    batch[key] = bar
            pending = later
            yield batch

    def _drain(self, pending: list[_Item]) -> None:
        while not self.queue.empty():
            pending.append(_item(*self.queue.get_nowait()))

    def _ready(self, pending: list[_Item], close_ms: int) -> bool:
        """Every stream expected at close_ms is queued, or a later bar already is."""
        present = {key for close, key, _ in pending if close == close_ms}
        expected = {key for key in self.keys if close_ms % key[1].millis == 0}
        return expected <= present or any(close > close_ms for close, _, _ in pending)


def _name(key: StreamKey) -> str:
    return f"{key[0]} {key[1]}"


def _fields(key: StreamKey) -> dict[str, str]:
    return {"symbol": key[0], "timeframe": str(key[1])}


def _host(url: str) -> str:
    return urlsplit(url).netloc


def _report[*Args](callback: Callable[[*Args], None], *args: *Args) -> None:
    """Run a report callback; its failure (a ledger busy for a moment) loses no bar."""
    try:
        callback(*args)
    except Exception as exc:
        log.error("report_failed", error=repr(exc))


def close_time(key: StreamKey, bar: pl.DataFrame) -> datetime:
    return datetime.fromtimestamp(
        (int(bar["open_time"].dt.epoch("ms")[0]) + key[1].millis) / 1000, UTC
    )


def bars_since(store: BarStore, key: StreamKey, count: int, now: datetime) -> pl.DataFrame:
    """Most recent `count` closed bars of a stream from the store."""
    symbol, timeframe = key
    end = timeframe.floor(now)
    return store.read(symbol, timeframe, end - timeframe.delta * count, end)
