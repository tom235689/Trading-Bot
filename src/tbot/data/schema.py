"""Bar schema and conversion from raw Binance kline rows."""

from collections.abc import Sequence

import polars as pl

# Column order of Binance kline archives and REST responses.
RAW_COLUMNS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
]

BAR_SCHEMA = pl.Schema(
    {
        "open_time": pl.Datetime("ms", "UTC"),
        "open": pl.Float64(),
        "high": pl.Float64(),
        "low": pl.Float64(),
        "close": pl.Float64(),
        "volume": pl.Float64(),
        "quote_volume": pl.Float64(),
        "trades": pl.Int64(),
        "taker_buy_volume": pl.Float64(),
        "taker_buy_quote_volume": pl.Float64(),
    }
)

# Spot archives switched to microseconds in 2025; no millisecond timestamp is this large.
MICROS_THRESHOLD = 10**14


def empty_bars() -> pl.DataFrame:
    return pl.DataFrame(schema=BAR_SCHEMA)


def from_raw(raw: pl.DataFrame) -> pl.DataFrame:
    """Convert a frame with RAW_COLUMNS (any dtype) to BAR_SCHEMA."""
    ts = pl.col("open_time").cast(pl.Int64)
    millis = pl.when(ts >= MICROS_THRESHOLD).then(ts // 1000).otherwise(ts)
    return raw.select(
        millis.cast(pl.Datetime("ms")).dt.replace_time_zone("UTC").alias("open_time"),
        *(pl.col(name).cast(dtype) for name, dtype in BAR_SCHEMA.items() if name != "open_time"),
    )


def from_rows(rows: Sequence[Sequence[object]]) -> pl.DataFrame:
    """Convert kline rows as returned by the REST API."""
    if not rows:
        return empty_bars()
    text = [[str(value) for value in row] for row in rows]
    raw = pl.DataFrame(text, schema=dict.fromkeys(RAW_COLUMNS, pl.String), orient="row")
    return from_raw(raw)
