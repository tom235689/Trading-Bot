"""Live feed of closed bars: WebSocket first, REST catch-up for anything missed.

Contract: every closed bar of every stream is emitted exactly once, in order,
after being written to the bar store. The store is the memory of what was seen.
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime

import httpx
import polars as pl
import structlog

from tbot.core.timeframe import Timeframe
from tbot.data import binance_ws
from tbot.data.binance_rest import fetch_klines
from tbot.data.quality import aligned
from tbot.data.store import BarStore

StreamKey = tuple[str, Timeframe]
Connector = Callable[[str], AbstractAsyncContextManager[AsyncIterator[str | bytes]]]
Batch = dict[StreamKey, pl.DataFrame]

log = structlog.get_logger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


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
        self.last: dict[StreamKey, datetime | None] = {
            key: store.last_open_time(*key) for key in self.keys
        }
        self.queue: asyncio.Queue[tuple[StreamKey, pl.DataFrame]] = asyncio.Queue()
        self.reconnects = 0
        self._stale_reported: set[StreamKey] = set()

    # emission

    def _emit(self, key: StreamKey, bars: pl.DataFrame, *, exchange_closed: bool = False) -> int:
        """Store and queue bars that are new, closed, and on the grid. Return the count.

        The exchange marks socket bars closed itself; REST bars are judged by the clock,
        which must be server-synced or a fast local clock stores a still-open bar.
        """
        timeframe = key[1]
        bars = bars.filter(aligned(timeframe))
        if not exchange_closed:
            bars = bars.filter(pl.col("open_time") < timeframe.floor(self.clock()))
        last = self.last[key]
        if last is not None:
            bars = bars.filter(pl.col("open_time") > last)
        if bars.is_empty():
            return 0
        bars = bars.sort("open_time")
        self.store.write(*key, bars)
        for row in bars.iter_slices(1):
            self.queue.put_nowait((key, row))
        self.last[key] = bars["open_time"][-1]
        self._stale_reported.discard(key)
        return bars.height

    async def catch_up(self, keys: Sequence[StreamKey] | None = None) -> int:
        """Fetch bars closed since the last emitted one via REST."""
        total = 0
        for key in keys or self.keys:
            symbol, timeframe = key
            last = self.last[key]
            end = timeframe.floor(self.clock())
            start = last + timeframe.delta if last is not None else end - timeframe.delta * 2
            if start >= end:
                continue
            bars = await asyncio.to_thread(fetch_klines, self.client, symbol, timeframe, start, end)
            count = self._emit(key, bars)
            if count:
                log.info("catch_up", symbol=symbol, timeframe=str(timeframe), bars=count)
            total += count
        return total

    # tasks

    async def run_websocket(self) -> None:
        url = binance_ws.stream_url(self.keys)
        backoff = 1.0
        while True:
            try:
                async with self.connector(url) as messages:
                    log.info("websocket_connected", streams=len(self.keys))
                    backoff = 1.0
                    await self.catch_up()
                    async for message in messages:
                        kline = binance_ws.parse_kline(message)
                        if kline is not None and kline.closed and kline.key in self.last:
                            self._emit(kline.key, kline.bar, exchange_closed=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # any transport failure: reconnect
                self.reconnects += 1
                log.warning("websocket_error", error=repr(exc), retry_in=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def run_watchdog(self) -> None:
        """Poll REST for bars the socket did not deliver; report streams that stay stale."""
        while True:
            await asyncio.sleep(self.poll_seconds)
            now = self.clock()
            overdue = [key for key in self.keys if self._overdue_seconds(key, now) > 0]
            if overdue:
                await self.catch_up(overdue)
            if self.on_stale is None:
                continue
            for key in self.keys:
                stale = self._overdue_seconds(key, self.clock()) > self.stale_after
                if stale and key not in self._stale_reported:
                    self._stale_reported.add(key)
                    self.on_stale(key, self.last[key] or datetime.min.replace(tzinfo=UTC))

    def _overdue_seconds(self, key: StreamKey, now: datetime) -> float:
        """Seconds since the latest closed bar should have arrived, if it has not."""
        timeframe = key[1]
        expected_open = timeframe.floor(now) - timeframe.delta
        last = self.last[key]
        if last is not None and last >= expected_open:
            return 0.0
        return (now - (expected_open + timeframe.delta)).total_seconds()

    # consumption

    async def batches(self) -> AsyncIterator[Batch]:
        """Group queued bars by close time so streams closing together form one event.

        Waits up to batch_wait seconds for the other streams expected at that close.
        """
        while True:
            key, bar = await self.queue.get()
            close_ms = int(bar["open_time"].dt.epoch("ms")[0]) + key[1].millis
            batch: Batch = {key: bar}
            deadline = asyncio.get_running_loop().time() + self.batch_wait
            while not self._complete(batch, close_ms):
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    key, bar = await asyncio.wait_for(self.queue.get(), remaining)
                except TimeoutError:
                    break
                bar_close = int(bar["open_time"].dt.epoch("ms")[0]) + key[1].millis
                if bar_close == close_ms and key not in batch:
                    batch[key] = bar
                else:
                    self.queue.put_nowait((key, bar))  # belongs to another event
                    if bar_close > close_ms:
                        break
            yield batch

    def _complete(self, batch: Batch, close_ms: int) -> bool:
        expected = {key for key in self.keys if close_ms % key[1].millis == 0}
        return expected <= set(batch)


def close_time(key: StreamKey, bar: pl.DataFrame) -> datetime:
    return datetime.fromtimestamp(
        (int(bar["open_time"].dt.epoch("ms")[0]) + key[1].millis) / 1000, UTC
    )


def bars_since(store: BarStore, key: StreamKey, count: int, now: datetime) -> pl.DataFrame:
    """Most recent `count` closed bars of a stream from the store."""
    symbol, timeframe = key
    end = timeframe.floor(now)
    return store.read(symbol, timeframe, end - timeframe.delta * count, end)
