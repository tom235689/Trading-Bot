"""Recent klines from the Binance public market data REST API."""

import json
from datetime import datetime

import httpx
import polars as pl

from tbot.core.timeframe import Timeframe, from_millis, to_millis
from tbot.data.http import get_bytes
from tbot.data.schema import empty_bars, from_rows

KLINES_URL = "https://data-api.binance.vision/api/v3/klines"
MAX_LIMIT = 1000


def fetch_klines(
    client: httpx.Client, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
) -> pl.DataFrame:
    """Fetch bars with open_time in [start, end)."""
    frames = []
    cursor, end_ms = to_millis(start), to_millis(end)
    while cursor < end_ms:
        params: dict[str, str | int] = {
            "symbol": symbol,
            "interval": str(timeframe),
            "startTime": cursor,
            "endTime": end_ms - 1,
            "limit": MAX_LIMIT,
        }
        body = get_bytes(client, KLINES_URL, params)
        if body is None:
            raise LookupError(f"not found: {KLINES_URL}")
        rows = json.loads(body)
        if not rows:
            break
        frames.append(from_rows(rows))
        if len(rows) < MAX_LIMIT:  # a short page is the last one
            break
        cursor = int(rows[-1][0]) + 1  # not one bar on: after an off-grid row that skips one
    if not frames:
        return empty_bars()
    return pl.concat(frames).filter(pl.col("open_time") >= start, pl.col("open_time") < end)


def first_open_time(
    client: httpx.Client, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
) -> datetime | None:
    """Open time of the earliest bar in [start, end), or None if Binance has none."""
    params: dict[str, str | int] = {
        "symbol": symbol,
        "interval": str(timeframe),
        "startTime": to_millis(start),
        "endTime": to_millis(end) - 1,
        "limit": 1,
    }
    body = get_bytes(client, KLINES_URL, params)
    if body is None:
        raise LookupError(f"not found: {KLINES_URL}")
    rows = json.loads(body)
    return from_millis(int(rows[0][0])) if rows else None
