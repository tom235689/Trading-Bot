import asyncio
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from binance_fake import make_bars
from tbot import __version__
from tbot.cli import EXIT_CONFIG, EXIT_ERROR, EXIT_LEDGER, build_parser, main, parse_symbol
from tbot.core.timeframe import Timeframe
from tbot.data.store import BarStore
from tbot.live.ledger import Ledger
from tbot.live.runner import (
    STOP_REQUEST_SECONDS,
    instance_lock,
    stop_path,
    take_stop_request,
    watch_stop_request,
)


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert __version__ in capsys.readouterr().out


def test_a_wrong_command_line_is_a_config_error() -> None:
    assert main(["status", "config/paper.yaml", "--bogus"]) == EXIT_CONFIG


def test_no_args() -> None:
    assert main([]) == 0


def test_parse_symbol() -> None:
    assert parse_symbol("btc/usdt") == "BTCUSDT"


def test_download_defaults() -> None:
    args = build_parser().parse_args(["download"])
    assert args.symbols == ["BTCUSDT", "ETHUSDT"]
    assert args.timeframes == [Timeframe.H1, Timeframe.H4]
    assert args.start == datetime(2017, 8, 1, tzinfo=UTC)


def test_check_ok(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    BarStore(tmp_path).write(
        "BTCUSDT", Timeframe.H4, make_bars(datetime(2024, 1, 1, tzinfo=UTC), 6, Timeframe.H4)
    )
    argv = ["check", "--symbols", "BTCUSDT", "--timeframes", "4h", "--data-dir", str(tmp_path)]
    assert main(argv) == 0
    assert "BTCUSDT 4h: OK, 6 bars" in capsys.readouterr().out


def test_check_without_data_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["check", "--symbols", "ETHUSDT", "--timeframes", "1h", "--data-dir", str(tmp_path)]
    assert main(argv) == 1
    assert "ETHUSDT 1h: FAIL, no data" in capsys.readouterr().out


def test_backtest_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    BarStore(tmp_path).write(
        "BTCUSDT", Timeframe.H4, make_bars(datetime(2024, 1, 1, tzinfo=UTC), 60, Timeframe.H4)
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        "start: 2024-01-05\n"
        "strategies:\n"
        "  - {name: donchian_trend, symbols: [BTCUSDT], timeframe: 4h, allocation: 1.0}\n",
        encoding="utf-8",
    )
    assert main(["backtest", str(config), "--data-dir", str(tmp_path)]) == 0
    assert "CAGR" in capsys.readouterr().out


def test_notify_needs_telegram_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env here
    for name in ("TBOT_TELEGRAM_TOKEN", "TBOT_TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    assert main(["notify"]) == 1
    assert "TBOT_TELEGRAM_TOKEN" in capsys.readouterr().out


def test_wrong_kind_of_config_is_explained(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["paper", "config/donchian_voltarget.yaml"]) == EXIT_CONFIG
    assert "a backtest config" in capsys.readouterr().err
    assert main(["live", "config/testnet.yaml", "--live"]) == EXIT_CONFIG
    assert "only for mode: live" in capsys.readouterr().err


def test_errors_end_in_one_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    empty = str(tmp_path / "empty")
    assert main(["backtest", "config/donchian_voltarget.yaml", "--data-dir", empty]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert err.startswith("error: no stored bars for BTCUSDT 4h; run `tbot download`")
    assert "Traceback" not in err

    unknown = tmp_path / "unknown.yaml"
    unknown.write_text(
        "start: 2024-01-01\nstrategies:\n"
        "  - {name: nope, symbols: [BTCUSDT], timeframe: 4h, allocation: 1.0}\n",
        encoding="utf-8",
    )
    assert main(["backtest", str(unknown), "--data-dir", empty]) == EXIT_ERROR
    assert "unknown strategy 'nope'" in capsys.readouterr().err

    paper = tmp_path / "paper.yaml"
    broken = tmp_path / "broken.sqlite"
    broken.write_text("not a database", encoding="utf-8")
    paper.write_text(
        f"ledger: {broken.as_posix()}\nstrategies:\n"
        "  - {name: donchian_trend, symbols: [BTCUSDT], timeframe: 4h, allocation: 1.0}\n",
        encoding="utf-8",
    )
    assert main(["status", str(paper)]) == EXIT_LEDGER
    assert "not a usable tbot ledger" in capsys.readouterr().err


def test_env_that_is_not_utf8_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_bytes("TBOT_TELEGRAM_TOKEN=x\n".encode("utf-16"))
    assert main(["notify"]) == EXIT_CONFIG  # retrying cannot fix the file
    assert "save .env and configs as UTF-8" in capsys.readouterr().err


def test_stop_asks_a_running_session(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger = tmp_path / "paper.sqlite"
    Ledger(ledger).close()
    config = tmp_path / "paper.yaml"
    config.write_text(
        f"ledger: {ledger.as_posix()}\nstrategies:\n"
        "  - {name: donchian_trend, symbols: [BTCUSDT], timeframe: 4h, allocation: 1.0}\n",
        encoding="utf-8",
    )
    assert main(["stop", str(config)]) == 0
    assert "no session is running" in capsys.readouterr().out
    assert take_stop_request(ledger)  # a supervisor's restart would stop at once
    assert not stop_path(ledger).exists()
    stop_path(ledger).write_text("stop", encoding="utf-8")
    old = time.time() - STOP_REQUEST_SECONDS - 60
    os.utime(stop_path(ledger), (old, old))
    assert not take_stop_request(ledger)  # left from long ago: dropped, not obeyed
    assert not stop_path(ledger).exists()
    with instance_lock(ledger):  # a session holds the ledger and does not react in time
        assert main(["stop", str(config), "--timeout", "1"]) == EXIT_ERROR
    assert stop_path(ledger).exists()

    stop = asyncio.Event()
    asyncio.run(watch_stop_request(stop_path(ledger), stop))
    assert stop.is_set()
    assert not stop_path(ledger).exists()


def test_notify_finds_the_chat_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TBOT_TELEGRAM_TOKEN", "token")
    monkeypatch.delenv("TBOT_TELEGRAM_CHAT_ID", raising=False)
    result: list[dict[str, object]] = []

    async def updates(self: object, offset: int | None, timeout: int) -> list[dict[str, object]]:
        return result

    monkeypatch.setattr("tbot.cli.Telegram.updates", updates)
    assert main(["notify"]) == EXIT_ERROR
    assert "send your bot any message" in capsys.readouterr().out
    result.append({"update_id": 1, "message": {"chat": {"id": 12345, "first_name": "Tom"}}})
    assert main(["notify"]) == EXIT_ERROR
    out = capsys.readouterr().out
    assert "chat 12345 Tom" in out
    assert "TBOT_TELEGRAM_CHAT_ID" in out


def test_a_stop_request_can_be_withdrawn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "paper.sqlite"
    config = tmp_path / "paper.yaml"
    config.write_text(
        f"ledger: {ledger.as_posix()}\nstrategies:\n"
        "  - {name: donchian_trend, symbols: [BTCUSDT], timeframe: 4h, allocation: 1.0}\n",
        encoding="utf-8",
    )
    assert main(["stop", str(config), "--cancel"]) == 0
    assert "no stop request" in capsys.readouterr().out
    stop_path(ledger).write_text("stop\n", encoding="utf-8")
    assert main(["stop", str(config), "--cancel"]) == 0
    assert "withdrawn" in capsys.readouterr().out
    assert not stop_path(ledger).exists()


def test_each_config_logs_to_its_own_file() -> None:
    from tbot.cli import log_path

    assert log_path(Path("config/paper.yaml")) == Path("logs/paper.jsonl")
    assert log_path(Path("config/paper-eth.yaml")) == Path("logs/paper-eth.jsonl")


def test_download_stops_at_a_rate_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import httpx

    calls: list[str] = []
    request = httpx.Request("GET", "https://data-api.binance.vision/api/v3/klines")

    def limited(
        store: object, client: object, symbol: str, *args: object, **kwargs: object
    ) -> None:
        calls.append(symbol)
        response = httpx.Response(429, headers={"Retry-After": "90"}, request=request)
        raise httpx.HTTPStatusError("429", request=request, response=response)

    monkeypatch.setattr("tbot.cli.sync", limited)
    monkeypatch.setattr("tbot.cli.ServerClock.sync", lambda self: 0.0)
    assert main(["download", "--data-dir", str(tmp_path)]) == EXIT_ERROR
    assert calls == ["BTCUSDT"]  # no more requests: they could get the IP banned
    assert "run it again in 90 s" in capsys.readouterr().err
