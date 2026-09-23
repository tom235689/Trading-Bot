"""Monthly spot kline archives from data.binance.vision."""

import hashlib
import io
import zipfile

import httpx
import polars as pl

from tbot.core.timeframe import Timeframe
from tbot.data.http import get_bytes
from tbot.data.schema import RAW_COLUMNS, empty_bars, from_raw

ARCHIVE_URL = "https://data.binance.vision/data/spot/monthly/klines"


class ChecksumError(Exception):
    """Archive content does not match its published checksum."""


def monthly_url(symbol: str, timeframe: Timeframe, year: int, month: int) -> str:
    return f"{ARCHIVE_URL}/{symbol}/{timeframe}/{symbol}-{timeframe}-{year:04d}-{month:02d}.zip"


def fetch_month(
    client: httpx.Client, symbol: str, timeframe: Timeframe, year: int, month: int
) -> pl.DataFrame | None:
    """Download and verify one monthly archive. Return None if it is not published."""
    url = monthly_url(symbol, timeframe, year, month)
    archive = get_bytes(client, url)
    if archive is None:
        return None
    checksum = get_bytes(client, f"{url}.CHECKSUM")
    if checksum is None:
        raise ChecksumError(f"checksum missing: {url}")
    if hashlib.sha256(archive).hexdigest() != checksum.split()[0].decode().lower():
        raise ChecksumError(f"checksum mismatch: {url}")
    return parse_archive(archive)


def parse_archive(archive: bytes) -> pl.DataFrame:
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        names = [name for name in zf.namelist() if name.endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"expected one csv in archive, got {names}")
        return parse_csv(zf.read(names[0]))


def parse_csv(data: bytes) -> pl.DataFrame:
    """Parse kline CSV. Some Binance files start with a header row."""
    if not data.strip():
        return empty_bars()
    raw = pl.read_csv(
        io.BytesIO(data),
        has_header=not data[:1].isdigit(),
        new_columns=RAW_COLUMNS,
        infer_schema=False,
    )
    return from_raw(raw)
