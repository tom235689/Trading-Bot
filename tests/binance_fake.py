"""In-memory fake of the Binance archive and REST endpoints."""

import hashlib
import io
import zipfile
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any

import httpx
import polars as pl

from tbot.core.timeframe import Timeframe, to_millis
from tbot.data.schema import RAW_COLUMNS, from_rows

Row = list[Any]


def make_rows(start: datetime, count: int, timeframe: Timeframe, price: float = 100.0) -> list[Row]:
    """Kline rows shaped like Binance REST output."""
    rows = []
    for i in range(count):
        open_ms = to_millis(start) + i * timeframe.millis
        close_ms = open_ms + timeframe.millis - 1
        p = f"{price:.2f}"
        hi, lo = f"{price + 1:.2f}", f"{price - 1:.2f}"
        rows.append([open_ms, p, hi, lo, p, "10.0", close_ms, "1000.0", 5, "4.0", "400.0", "0"])
    return rows


def make_bars(start: datetime, count: int, timeframe: Timeframe) -> pl.DataFrame:
    return from_rows(make_rows(start, count, timeframe))


def to_csv(rows: list[Row], *, micros: bool = False, header: bool = False) -> bytes:
    lines = [",".join(RAW_COLUMNS)] if header else []
    for row in rows:
        values = list(row)
        if micros:
            values[0] = int(values[0]) * 1000
            values[6] = int(values[6]) * 1000 + 999
        lines.append(",".join(str(value) for value in values))
    return ("\n".join(lines) + "\n").encode()


def to_zip(name: str, data: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        # Fixed timestamp keeps the bytes, and so the checksum, stable.
        zf.writestr(zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0)), data)
    return buffer.getvalue()


def month_of(open_ms: int) -> tuple[int, int]:
    moment = datetime.fromtimestamp(open_ms / 1000, UTC)
    return moment.year, moment.month


class FakeBinance:
    """Serves rows as monthly archives and REST klines, and records requested URLs."""

    def __init__(
        self,
        symbol: str,
        timeframe: Timeframe,
        rows: list[Row],
        *,
        unpublished: Collection[tuple[int, int]] = (),
        micros: bool = False,
        bad_checksum: bool = False,
    ) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.rows = rows
        self.unpublished = set(unpublished)
        self.micros = micros
        self.bad_checksum = bad_checksum
        self.requests: list[str] = []

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def archive_requests(self) -> list[str]:
        return [url for url in self.requests if url.endswith(".zip")]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        if request.url.host == "data.binance.vision":
            return self._archive(request.url.path)
        if request.url.host == "data-api.binance.vision":
            return self._klines(request.url.params)
        return httpx.Response(404)

    def _archive(self, path: str) -> httpx.Response:
        name = path.rsplit("/", 1)[-1]
        zip_name = name.removesuffix(".CHECKSUM")
        symbol, timeframe, year, month = zip_name.removesuffix(".zip").split("-")
        key = (int(year), int(month))
        rows = [row for row in self.rows if month_of(row[0]) == key]
        wrong = (symbol, timeframe) != (self.symbol, str(self.timeframe))
        if wrong or key in self.unpublished or not rows:
            return httpx.Response(404)
        csv_name = zip_name.replace(".zip", ".csv")
        archive = to_zip(csv_name, to_csv(rows, micros=self.micros))
        if not name.endswith(".CHECKSUM"):
            return httpx.Response(200, content=archive)
        digest = "0" * 64 if self.bad_checksum else hashlib.sha256(archive).hexdigest()
        return httpx.Response(200, content=f"{digest}  {zip_name}\n".encode())

    def _klines(self, params: httpx.QueryParams) -> httpx.Response:
        start, end = int(params["startTime"]), int(params["endTime"])
        rows = [row for row in self.rows if start <= row[0] <= end]
        return httpx.Response(200, json=rows[: int(params["limit"])])
