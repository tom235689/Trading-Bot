from datetime import UTC, datetime

import pytest

from binance_fake import FakeBinance, make_rows, to_csv
from tbot.core.timeframe import Timeframe
from tbot.data.binance_vision import ChecksumError, fetch_month, monthly_url, parse_csv
from tbot.data.schema import BAR_SCHEMA, from_rows

START = datetime(2024, 1, 1, tzinfo=UTC)
ROWS = make_rows(START, 3, Timeframe.H4)


def test_monthly_url() -> None:
    assert monthly_url("BTCUSDT", Timeframe.H4, 2024, 3) == (
        "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/4h/BTCUSDT-4h-2024-03.zip"
    )


@pytest.mark.parametrize(("micros", "header"), [(False, False), (True, False), (False, True)])
def test_parse_csv_formats(micros: bool, header: bool) -> None:
    bars = parse_csv(to_csv(ROWS, micros=micros, header=header))
    assert bars.schema == BAR_SCHEMA
    assert bars.equals(from_rows(ROWS))
    assert bars["open_time"][0] == START


def test_parse_empty_csv() -> None:
    bars = parse_csv(b"")
    assert bars.is_empty()
    assert bars.schema == BAR_SCHEMA


def test_fetch_month_verifies_and_parses() -> None:
    fake = FakeBinance("BTCUSDT", Timeframe.H4, ROWS, micros=True)
    with fake.client() as client:
        bars = fetch_month(client, "BTCUSDT", Timeframe.H4, 2024, 1)
    assert bars is not None
    assert bars.equals(from_rows(ROWS))


def test_fetch_month_missing_returns_none() -> None:
    fake = FakeBinance("BTCUSDT", Timeframe.H4, ROWS)
    with fake.client() as client:
        assert fetch_month(client, "BTCUSDT", Timeframe.H4, 2024, 2) is None


def test_fetch_month_rejects_bad_checksum() -> None:
    fake = FakeBinance("BTCUSDT", Timeframe.H4, ROWS, bad_checksum=True)
    with fake.client() as client, pytest.raises(ChecksumError, match="mismatch"):
        fetch_month(client, "BTCUSDT", Timeframe.H4, 2024, 1)
