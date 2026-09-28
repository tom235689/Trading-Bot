import json
from datetime import UTC, datetime

from tbot.core.timeframe import Timeframe
from tbot.data.binance_ws import parse_kline, stream_url

MESSAGE = {
    "stream": "btcusdt@kline_4h",
    "data": {
        "e": "kline",
        "E": 1704081600123,
        "s": "BTCUSDT",
        "k": {
            "t": 1704067200000,
            "T": 1704081599999,
            "s": "BTCUSDT",
            "i": "4h",
            "f": 1,
            "L": 2,
            "o": "42000.10",
            "c": "42500.00",
            "h": "42600.00",
            "l": "41900.00",
            "v": "12.5",
            "n": 300,
            "x": True,
            "q": "525000.0",
            "V": "6.0",
            "Q": "252000.0",
            "B": "0",
        },
    },
}


def test_parse_closed_kline() -> None:
    kline = parse_kline(json.dumps(MESSAGE))
    assert kline is not None
    assert kline.key == ("BTCUSDT", Timeframe.H4)
    assert kline.closed
    row = kline.bar.row(0, named=True)
    assert row["open_time"] == datetime(2024, 1, 1, tzinfo=UTC)
    assert (row["open"], row["close"], row["trades"]) == (42000.10, 42500.0, 300)


def test_parse_ignores_other_messages() -> None:
    assert parse_kline(json.dumps({"result": None, "id": 1})) is None
    assert parse_kline(b'{"stream":"x","data":{"e":"trade"}}') is None


def test_stream_url() -> None:
    url = stream_url([("BTCUSDT", Timeframe.H1), ("ETHUSDT", Timeframe.H4)])
    assert url == "wss://stream.binance.com:9443/stream?streams=btcusdt@kline_1h/ethusdt@kline_4h"
