"""Live feed of closed bars: WebSocket first, REST catch-up for anything missed.

Contract: every closed bar of every stream is emitted exactly once, in order,
after being written to the bar store. The store is the memory of what was seen.
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta

import httpx
import polars as pl
import structlog

from tbot.core.timeframe import Timeframe
from tbot.data import binance_ws
from tbot.data.binance_rest import fetch_klines
from tbot.data.http import retry_after
from tbot.data.quality import aligned
from tbot.data.store import BarStore
from tbot.live.clock import CLOSE_GRACE

StreamKey = tuple[str, Timeframe]
Connector = Callable[[str], AbstractAsyncContextManager[AsyncIterator[str | bytes]]]
Batch = dict[StreamKey, pl.DataFrame]

log = structlog.get_logger(__name__)
FETCH_ERRORS = (httpx.HTTPError, LookupError, ValueError, OSError)  # REST failures, retried
HEALTHY_SECONDS = 60.0  # a socket open this long resets the reconnect backoff


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
        self.last: dict[StreamKey, datetime | None] = {
            key: last[key] if last is not None else store.last_open_time(*key) for key in self.keys
        }
        self.queue: asyncio.Queue[tuple[StreamKey, pl.DataFrame]] = asyncio.Queue()
        self.reconnects = 0
        self._stale_reported: set[StreamKey] = set()
        self.rest_paused_until: datetime | None = None  # a rate limit: calling now risks a ban

    # emission

    def _emit(self, key: StreamKey, bars: pl.DataFrame, *, exchange_closed: bool = False) -> int:
        """Store and queue bars that are new, closed, and on the grid. Return the count."""
        accepted = self._accept(key, bars, exchange_closed=exchange_closed)
        for row in accepted.iter_slices(1):
            self.queue.put_nowait((key, row))
        return accepted.height

    def _accept(
        self, key: StreamKey, bars: pl.DataFrame, *, exchange_closed: bool = False
    ) -> pl.DataFrame:
        """Store the bars that are new, closed, and on the grid, and return them.

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
        self.store.write(*key, bars)
        self.last[key] = bars["open_time"][-1]
        if key in self._stale_reported:
            self._stale_reported.discard(key)
            if self.on_fresh is not None:
                self.on_fresh(key)
        return bars

    def stale(self) -> list[StreamKey]:
        """Streams reported stale that have not delivered a bar since."""
        return [key for key in self.keys if key in self._stale_reported]

    async def catch_up(self, keys: Sequence[StreamKey] | None = None) -> int:
        """Fetch bars closed since the last emitted one via REST.

        Every stream is fetched before any bar is stored or queued, then they are queued
        in close order with no await in between: a 1h bar queued ahead of the 4h bar
        closing with it would release that event without it. A stream whose fetch fails
        stays behind and is asked again later; the others go on, and the first failure
        is raised once they are queued.
        """
        if self.rest_paused_until is not None and self.clock() < self.rest_paused_until:
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
        try:
            for key, bars in fetched:
                accepted = self._accept(key, bars)  # stored and marked seen: must be queued
                if accepted.height:
                    log.info("catch_up", symbol=key[0], timeframe=str(key[1]), bars=accepted.height)
                items.extend(_item(key, row) for row in accepted.iter_slices(1))
        finally:
            items.sort(key=lambda item: (item[0], item[1][1].millis, item[1][0]))
            for _, key, row in items:
                self.queue.put_nowait((key, row))
        if failure is not None:
            raise failure
        return len(items)

    async def _catch_up_or_log(self, keys: Sequence[StreamKey] | None = None) -> None:
        """Catch up; a stream REST cannot serve now is left behind for the watchdog."""
        try:
            await self.catch_up(keys)
        except FETCH_ERRORS as exc:
            log.warning("catch_up_incomplete", error=repr(exc))

    # tasks

    async def run_websocket(self) -> None:
        url = binance_ws.stream_url(self.keys)
        backoff = 1.0
        loop = asyncio.get_running_loop()
        while True:
            opened = loop.time()
            try:
                async with self.connector(url) as messages:
                    log.info("websocket_connected", streams=len(self.keys))
                    await self._catch_up_or_log()
                    async for message in messages:
                        kline = binance_ws.parse_kline(message)
                        if kline is not None and kline.closed and kline.key in self.last:
                            await self._on_socket_bar(kline.key, kline.bar)
                error = "closed by the server"  # a clean close: reconnect as after an error
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # any transport failure: reconnect
                error = repr(exc)
            if loop.time() - opened > HEALTHY_SECONDS:
                backoff = 1.0  # it worked for a while: a new failure, not the same one
            self.reconnects += 1
            log.warning("websocket_error", error=error, retry_in=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _on_socket_bar(self, key: StreamKey, bar: pl.DataFrame) -> None:
        """Emit a socket bar only if it follows the last one; a gap is filled via REST first.

        Emitting it straight away would leave a hole in the store and the history
        that no later catch-up could fill, since only bars after the last one count.
        """
        if self._gap_before(key, bar):
            await self._catch_up_or_log([key])
        if not self._gap_before(key, bar):
            self._emit(key, bar, exchange_closed=True)

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
                    self.on_stale(key, self.last[key] or datetime.min.replace(tzinfo=UTC))

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


def close_time(key: StreamKey, bar: pl.DataFrame) -> datetime:
    return datetime.fromtimestamp(
        (int(bar["open_time"].dt.epoch("ms")[0]) + key[1].millis) / 1000, UTC
    )


def bars_since(store: BarStore, key: StreamKey, count: int, now: datetime) -> pl.DataFrame:
    """Most recent `count` closed bars of a stream from the store."""
    symbol, timeframe = key
    end = timeframe.floor(now)
    return store.read(symbol, timeframe, end - timeframe.delta * count, end)
