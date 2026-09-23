"""Recent klines from the Binance public market data REST API."""

import json
from datetime import datetime

import httpx
import polars as pl

from tbot.core.timeframe import Timeframe, to_millis
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
        cursor = int(rows[-1][0]) + timeframe.millis
    if not frames:
        return empty_bars()
    return pl.concat(frames).filter(pl.col("open_time") >= start, pl.col("open_time") < end)
