"""Binance kline WebSocket stream: URL, message parsing, connection."""

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import polars as pl
import websockets

from tbot.core.timeframe import Timeframe
from tbot.data.schema import from_rows

STREAM_URL = "wss://stream.binance.com:9443/stream"
StreamKey = tuple[str, Timeframe]


@dataclass(frozen=True)
class Kline:
    key: StreamKey
    closed: bool
    bar: pl.DataFrame  # one row in BAR_SCHEMA


def stream_name(key: StreamKey) -> str:
    symbol, timeframe = key
    return f"{symbol.lower()}@kline_{timeframe}"


def stream_url(keys: Sequence[StreamKey], base: str = STREAM_URL) -> str:
    return f"{base}?streams={'/'.join(stream_name(key) for key in keys)}"


def parse_kline(message: str | bytes) -> Kline | None:
    """Parse a combined-stream kline message; other messages give None."""
    payload: dict[str, Any] = json.loads(message)
    data = payload.get("data", payload)
    if data.get("e") != "kline":
        return None
    k = data["k"]
    row = [
        k["t"],
        k["o"],
        k["h"],
        k["l"],
        k["c"],
        k["v"],
        k["T"],
        k["q"],
        k["n"],
        k["V"],
        k["Q"],
        "0",
    ]
    return Kline(key=(k["s"], Timeframe(k["i"])), closed=bool(k["x"]), bar=from_rows([row]))


async def _messages(ws: websockets.ClientConnection) -> AsyncIterator[str | bytes]:
    async for message in ws:
        yield message


@asynccontextmanager
async def connect(url: str) -> AsyncIterator[AsyncIterator[str | bytes]]:
    """Yield an iterator of raw messages. Binance drops connections after 24h; callers reconnect."""
    async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_queue=1024) as ws:
        yield _messages(ws)
