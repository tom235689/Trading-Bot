import argparse
import asyncio
import io
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import polars as pl
import pytest
import yaml
from pydantic import ValidationError

from binance_fake import make_bars
from factories import price_bars
from tbot import __version__
from tbot.backtest.config import BacktestConfig, load_config
from tbot.cli import (
    COMMANDS,
    EXIT_CONFIG,
    EXIT_ERROR,
    EXIT_LEDGER,
    announce,
    build_parser,
    check_holdout,
    check_period,
    config_arg,
    config_names,
    load_session_config,
    main,
    not_running,
    output_file,
    parse_symbol,
    parse_timeframe,
    pick_config,
    print_notes,
    run,
    session_logging,
    write_dashboard,
)
from tbot.core.config import StrategyConfig, load_yaml
from tbot.core.timeframe import Timeframe
from tbot.data.store import BarStore
from tbot.live.commands import PollTurn
from tbot.live.config import Settings, save_settings
from tbot.live.ledger import EquityPoint, Ledger
from tbot.live.runner import (
    GUARD_META,
    STOP_REQUEST_SECONDS,
    instance_lock,
    stop_path,
    take_stop_request,
    watch_stop_request,
)
from tbot.monitoring.dashboard import DashboardData
from tbot.monitoring.telegram import Telegram
from tbot.research.validate import ValidationConfig
from tbot.risk.guard import GuardState


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


def test_check_finds_a_1m_bar_still_open(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    BarStore(tmp_path).write(
        "BTCUSDT", Timeframe.M1, make_bars(now - timedelta(minutes=9), 10, Timeframe.M1)
    )
    argv = ["check", "--symbols", "BTCUSDT", "--timeframes", "1m", "--data-dir", str(tmp_path)]
    assert main(argv) == 1
    assert "error: 1 incomplete" in capsys.readouterr().out


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
    *progress, error = err.splitlines()
    assert progress == ["running the backtest..."]
    assert error.startswith("error: no stored bars for BTCUSDT 4h; run `tbot download --symbols")
    assert f'--data-dir "{empty}"`' in err  # the store it looked in
    assert "Traceback" not in err

    unknown = tmp_path / "unknown.yaml"
    unknown.write_text(
        "start: 2024-01-01\nstrategies:\n"
        "  - {name: nope, symbols: [BTCUSDT], timeframe: 4h, allocation: 1.0}\n",
        encoding="utf-8",
    )
    assert main(["backtest", str(unknown), "--data-dir", empty]) == EXIT_CONFIG  # before it runs
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
    elsewhere = {log_path(Path(folder) / "paper.yaml") for folder in ("a", "b", "config")}
    assert len(elsewhere) == 3  # the same name in another folder: another log


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


STRATEGY = "  - {name: donchian_trend, symbols: [BTCUSDT], timeframe: 4h, allocation: 1.0}\n"


def write_configs(root: Path) -> None:
    """A paper and a testnet session, and a backtest config that is no session."""
    folder = root / "config"
    folder.mkdir()
    (folder / "paper.yaml").write_text(f"ledger: paper.sqlite\nstrategies:\n{STRATEGY}", "utf-8")
    (folder / "testnet.yaml").write_text(
        f"mode: testnet\nledger: testnet.sqlite\nstrategies:\n{STRATEGY}", "utf-8"
    )
    (folder / "trend.yaml").write_text(f"start: 2024-01-01\nstrategies:\n{STRATEGY}", "utf-8")


def test_a_config_can_be_named(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert config_arg("paper") == Path("config/paper.yaml")
    assert config_arg("config/testnet.yaml") == Path("config/testnet.yaml")
    assert config_arg("nope") == Path("nope")
    assert build_parser().parse_args(["paper"]).config == Path("config/paper.yaml")
    assert build_parser().parse_args(["backtest", "donchian_voltarget"]).config == Path(
        "config/donchian_voltarget.yaml"
    )
    assert main(["status", "papr"]) == EXIT_CONFIG
    err = capsys.readouterr().err
    assert err.startswith("papr: no such config (names in config/: ")
    assert "paper, testnet" in err


def test_session_commands_find_the_config_they_mean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    paper, testnet = Path("config/paper.yaml"), Path("config/testnet.yaml")
    assert pick_config("doctor") == paper  # nothing has run: paper
    assert pick_config("account", live=True) == testnet  # the only testnet or live config
    assert capsys.readouterr().out == f"using {paper}\nusing {testnet}\n"
    Ledger(tmp_path / "testnet.sqlite").close()
    assert pick_config("backup") == testnet  # the only one that has run
    Ledger(tmp_path / "paper.sqlite").close()
    assert main(["backup"]) == EXIT_CONFIG  # two have run: never a guess
    assert "name the config (with a ledger: paper, testnet), for example: tbot backup paper" in (
        capsys.readouterr().err
    )
    with instance_lock(tmp_path / "testnet.sqlite"):
        assert pick_config("log") == testnet  # the running one
        assert main(["stop", "--timeout", "0"]) == EXIT_ERROR  # asked; it did not react
        assert "still running: `tbot log testnet`" in capsys.readouterr().err
    assert main(["stop"]) == 0
    assert capsys.readouterr().out == (
        "no session from config/ is running (another file: tbot stop <file>)\n"
    )
    assert main(["stop", "--cancel"]) == 0
    assert capsys.readouterr().out == "stop request for testnet withdrawn\n"

    assert main(["resume"]) == 0
    assert capsys.readouterr().out == "no session is halted\n"
    with Ledger(tmp_path / "paper.sqlite") as ledger:
        state = GuardState(peak_equity=100.0, halted=True, halt_reason="drawdown 50%")
        ledger.set_meta(GUARD_META, state.model_dump_json())
    assert main(["resume"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"using {paper}",
        "halted: drawdown 50%",
        "after resume the kill switch counts from the equity then: another 45% fall from here "
        "halts it again; the strategy may buy back in at the next bar",
        "resumed (was halted: drawdown 50%); a running bot trades again from its next bar",
    ]


def test_status_without_a_config_shows_every_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    assert main(["status"]) == 0
    assert capsys.readouterr().out.splitlines()[:4] == [
        f"tbot {__version__}",
        "config   state",
        "paper    not started",
        "testnet  not started",
    ]
    with Ledger(tmp_path / "paper.sqlite") as ledger:
        ledger.add_equity(EquityPoint(datetime(2026, 10, 1, tzinfo=UTC), 10_250.0, 10_250.0, 0.0))
    assert main(["status"]) == 0
    row = capsys.readouterr().out.splitlines()[2].split()
    assert row[:5] == ["paper", "stopped", "10,250.00", "+250.00", "(+2.5%)"]  # from 10,000


def test_paper_says_how_to_stop_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    config = load_session_config(Path("config/paper.yaml"))
    announce("paper", Path("config/paper.yaml"), config, Path("logs/paper.jsonl"))
    assert "`tbot stop paper` from another terminal" in capsys.readouterr().err
    elsewhere = tmp_path / "other" / "mine.yaml"
    announce("paper", elsewhere, config, Path("logs/mine.jsonl"))
    assert f"`tbot stop {elsewhere}`" in capsys.readouterr().err


def test_help_starts_with_the_first_steps(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "start here:" in out
    assert "tbot paper          paper trade with config/paper.yaml until stopped" in out


def test_log_shows_readable_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    assert main(["log", "papr"]) == EXIT_CONFIG  # a typo: no log to wait for
    assert capsys.readouterr().err.startswith("papr: no such config")
    assert main(["log", "paper"]) == EXIT_ERROR
    assert (
        capsys.readouterr().out
        == f"no log at {Path('logs/paper.jsonl')} yet: a session writes it while it runs\n"
    )
    path = tmp_path / "logs" / "paper.jsonl"
    path.parent.mkdir()
    lines = [
        {"event": "clock_synced", "level": "info", "timestamp": "2026-10-08T10:06:21Z"},
        {"event": "stale_stream", "level": "warning", "timestamp": "2026-10-08T10:07:00Z"},
    ]
    path.write_bytes(b"".join(json.dumps(line).encode() + b"\n" for line in lines))
    assert main(["log", "paper", "-n", "5"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "2026-10-08 10:06:21 info    clock_synced",
        "2026-10-08 10:07:00 warning stale_stream",
    ]
    assert main(["log", "--file", str(path), "--level", "error"]) == 0
    assert "has no lines at level error or above" in capsys.readouterr().out


def test_save_settings_keeps_the_rest_of_the_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_bytes(b"# TBOT_TELEGRAM_TOKEN=commented\r\nTBOT_TELEGRAM_TOKEN = old\r\nOTHER=1\r\n")
    save_settings(env, {"TBOT_TELEGRAM_TOKEN": "123:abc", "TBOT_TELEGRAM_CHAT_ID": "42"})
    assert env.read_bytes() == (
        b"# TBOT_TELEGRAM_TOKEN=commented\r\nTBOT_TELEGRAM_TOKEN=123:abc\r\nOTHER=1\r\n"
        b"TBOT_TELEGRAM_CHAT_ID=42\r\n"
    )
    fresh = tmp_path / "fresh" / ".env"
    fresh.parent.mkdir()
    (fresh.parent / ".env.example").write_bytes(b"# alerts\nTBOT_TELEGRAM_CHAT_ID=\n")
    save_settings(fresh, {"TBOT_TELEGRAM_CHAT_ID": "-100"})
    assert fresh.read_bytes() == b"# alerts\nTBOT_TELEGRAM_CHAT_ID=-100\n"
    bare = tmp_path / "bare" / ".env"
    bare.parent.mkdir()
    save_settings(bare, {"A": "1"})
    assert bare.read_bytes() == b"A=1\n"
    assert not list(tmp_path.rglob("*.partial"))


class FakeTelegram:
    """Telegram's answers, in order; records what the bot sent."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, inbox: list[list[dict[str, Any]]]):
        self.inbox = inbox
        self.sent: list[tuple[str, str]] = []
        self.status = 200
        fake = self

        async def me(bot: Telegram) -> str:
            if fake.status != 200:
                request = httpx.Request("GET", "https://api.telegram.org/getMe")
                response = httpx.Response(fake.status, request=request)
                raise httpx.HTTPStatusError("bad", request=request, response=response)
            return "tbot_test_bot"

        async def updates(bot: Telegram, offset: int | None, timeout: int) -> list[dict[str, Any]]:
            return fake.inbox.pop(0) if len(fake.inbox) > 1 else fake.inbox[0]

        async def send(bot: Telegram, text: str) -> bool:
            fake.sent.append((bot.chat_id, text))
            return True

        monkeypatch.setattr("tbot.cli.Telegram.me", me)
        monkeypatch.setattr("tbot.cli.Telegram.updates", updates)
        monkeypatch.setattr("tbot.cli.Telegram.send", send)


CODE = "tb4f8a2e91"


def message(chat: int, name: str, text: str = "hi", age: float = 0.0) -> dict[str, Any]:
    sent = datetime.now(UTC).timestamp() - age
    return {
        "update_id": chat,
        "message": {"chat": {"id": chat, "first_name": name}, "text": text, "date": int(sent)},
    }


@pytest.fixture
def no_telegram(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    for name in ("TBOT_TELEGRAM_TOKEN", "TBOT_TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("tbot.cli.one_time_code", lambda: CODE)
    monkeypatch.setattr("tbot.cli.CHAT_WAIT_SECONDS", 0.2)
    (tmp_path / ".env.example").write_bytes(
        b"# Telegram\nTBOT_TELEGRAM_TOKEN=\nTBOT_TELEGRAM_CHAT_ID=\nTBOT_HEARTBEAT_URL=\n"
    )
    return tmp_path / ".env"


def test_notify_guides_a_first_setup_and_saves_it(
    no_telegram: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A stranger wrote to the bot before (the backlog, skipped), and sends text holding
    # the code and an old message; only the chat that sends the code gets the alerts.
    telegram = FakeTelegram(
        monkeypatch,
        [
            [message(7, "Stranger", CODE)],
            [message(8, "Stranger", f"all codes {CODE} tb000001")],
            [message(9, "Stranger", CODE, age=3600)],
            [message(42, "Tom", f" {CODE} ")],
        ],
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": "  123:abc  ")
    monkeypatch.setattr("tbot.cli.interactive", lambda: True)
    assert main(["notify"]) == 0
    assert no_telegram.read_bytes() == (
        b"# Telegram\nTBOT_TELEGRAM_TOKEN=123:abc\nTBOT_TELEGRAM_CHAT_ID=42\nTBOT_HEARTBEAT_URL=\n"
    )
    assert telegram.sent == [("42", "tbot: test alert. Alerts from the bot arrive in this chat.")]
    out = capsys.readouterr().out
    assert f"Now send this code to @tbot_test_bot in Telegram: {CODE}" in out
    assert "found chat 42 Tom" in out
    assert "saved chat 42 in .env" in out
    assert main(["notify"]) == 0  # set up: only the test message
    assert len(telegram.sent) == 2


def test_notify_picks_the_chat_again_and_waits_for_a_session_to_let_go(
    no_telegram: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    telegram = FakeTelegram(monkeypatch, [[message(77, "Group", f"/{CODE}@tbot_test_bot")]])
    monkeypatch.setenv("TBOT_TELEGRAM_TOKEN", "123:abc")
    monkeypatch.setenv("TBOT_TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr("tbot.cli.interactive", lambda: True)
    assert main(["notify"]) == 0  # set up: only a test message to 42
    assert telegram.sent[-1][0] == "42"

    session = PollTurn("123:abc", 0)  # a running session reads the bot's messages
    assert session.take()
    monkeypatch.setattr("tbot.cli.TURN_WAIT_SECONDS", 0.5)
    assert main(["notify", "--chat"]) == EXIT_ERROR
    assert "it did not let go: stop the session" in capsys.readouterr().out
    threading.Timer(0.3, session.release).start()  # its next poll sees the request
    monkeypatch.setattr("tbot.cli.TURN_WAIT_SECONDS", 10)
    assert main(["notify", "--chat"]) == 0
    out = capsys.readouterr().out
    assert "asking it to let go" in out
    assert "saved chat 77 in .env" in out
    assert telegram.sent[-1][0] == "77"
    assert session.take()  # notify gave the turn back
    session.release()


def test_notify_without_a_terminal_or_with_a_wrong_token(
    no_telegram: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    telegram = FakeTelegram(monkeypatch, [[message(1, "Tom", CODE)]])
    assert main(["notify"]) == EXIT_ERROR  # nobody to ask for the token
    assert "set TBOT_TELEGRAM_TOKEN (from @BotFather) in .env" in capsys.readouterr().out
    monkeypatch.setenv("TBOT_TELEGRAM_TOKEN", "123:abc")
    assert main(["notify"]) == EXIT_ERROR  # nobody to send the code: no chat is guessed
    assert "run `tbot notify` in a terminal to pick the chat" in capsys.readouterr().out
    assert not no_telegram.exists()
    monkeypatch.setattr("tbot.cli.interactive", lambda: True)
    telegram.inbox = [[message(1, "Tom"), message(2, "Group", "123456")]]  # no code: waits
    assert main(["notify"]) == EXIT_ERROR
    assert "the code did not reach @tbot_test_bot" in capsys.readouterr().out
    assert not no_telegram.exists()
    telegram.inbox = [[message(1, "Tom"), message(2, "Group", f"/{CODE}@tbot_test_bot")]]
    assert main(["notify"]) == 0
    assert "TBOT_TELEGRAM_CHAT_ID=2\n" in no_telegram.read_text("utf-8")
    assert "TBOT_TELEGRAM_TOKEN=\n" in no_telegram.read_text("utf-8")  # from the environment
    telegram.status = 401
    assert main(["notify"]) == EXIT_ERROR
    assert "Telegram does not know this token" in capsys.readouterr().out


def test_a_dashboard_opens_only_for_a_person(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    monkeypatch.setattr("tbot.cli.webbrowser.open", opened.append)
    monkeypatch.setattr("tbot.cli.render", lambda data: "<html></html>")
    out = tmp_path / "reports" / "paper.html"
    data = cast(DashboardData, None)
    write_dashboard(data, out, show=True)  # output captured: a script, not a person
    assert opened == []

    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdout", Terminal())
    write_dashboard(data, out, show=False)
    assert opened == []
    write_dashboard(data, out, show=True)
    assert opened == [out.resolve().as_uri()]


def test_a_config_that_cannot_be_read_is_named_and_hides_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    (tmp_path / "config" / "korean.yaml").write_bytes("ledger: x\n".encode("utf-16"))
    (tmp_path / "config" / "date.yaml").write_text("start: 2024-02-30\n", "utf-8")
    assert main(["status", "korean"]) == EXIT_CONFIG
    assert "korean.yaml: not UTF-8 text" in capsys.readouterr().err
    assert main(["status", "date"]) == EXIT_CONFIG
    assert "date.yaml: not valid YAML" in capsys.readouterr().err
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "paper    not started" in out
    assert "korean   invalid" in out


def test_stop_finds_a_session_whose_config_no_longer_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    Ledger(tmp_path / "paper.sqlite").close()
    with instance_lock(tmp_path / "paper.sqlite"):  # running on settings edited since
        (tmp_path / "config" / "paper.yaml").write_text(
            "ledger: paper.sqlite\nstrategies: []\n", "utf-8"
        )
        assert main(["stop", "--timeout", "0"]) == EXIT_ERROR  # asked; it did not react
        assert "asked the session on paper.sqlite to stop" in capsys.readouterr().out
    assert stop_path(tmp_path / "paper.sqlite").exists()
    assert main(["stop", "--cancel"]) == 0
    assert capsys.readouterr().out == "stop request for paper withdrawn\n"


def test_stop_reaches_a_supervisor_about_to_restart_the_only_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    Ledger(tmp_path / "testnet.sqlite").close()  # crashed; its supervisor waits
    assert main(["stop"]) == 0
    assert "no session is running on testnet.sqlite; a supervisor's restart" in (
        capsys.readouterr().out
    )
    assert stop_path(tmp_path / "testnet.sqlite").exists()


def test_resume_never_picks_a_real_money_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "live.yaml").write_text(
        f"mode: live\ninitial_cash: 1000\nledger: live.sqlite\nstrategies:\n{STRATEGY}", "utf-8"
    )
    with Ledger(tmp_path / "live.sqlite") as ledger:
        state = GuardState(peak_equity=100.0, halted=True, halt_reason="drawdown 50%")
        ledger.set_meta(GUARD_META, state.model_dump_json())
    assert main(["resume"]) == EXIT_CONFIG
    assert "trades real money: name it to resume it: tbot resume live" in capsys.readouterr().err
    assert main(["resume", "live"]) == 0  # named: done
    assert "resumed" in capsys.readouterr().out


def test_ctrl_c_ends_a_command_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(args: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setitem(COMMANDS, "status", interrupted)
    assert main(["status"]) == EXIT_ERROR
    assert capsys.readouterr().err == "interrupted\n"


def test_save_settings_sets_every_copy_of_a_key(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_bytes(b"TBOT_TELEGRAM_CHAT_ID=1\nTBOT_TELEGRAM_CHAT_ID=\n")
    save_settings(env, {"TBOT_TELEGRAM_CHAT_ID": "42"})
    assert env.read_bytes() == b"TBOT_TELEGRAM_CHAT_ID=42\nTBOT_TELEGRAM_CHAT_ID=42\n"


def test_log_with_no_lines_asked_for_prints_only_new_ones(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "paper.jsonl"
    path.write_bytes(b'{"event": "old", "level": "info"}\n')
    assert main(["log", "--file", str(path), "-n", "0"]) == 0
    assert capsys.readouterr().out == ""


def test_backtest_notes_symbols_that_trade_on_different_bars(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = BacktestConfig.model_validate(
        {
            "start": "2024-01-01",
            "strategies": [
                {
                    "name": "donchian_trend",
                    "symbols": ["BTCUSDT", "ETHUSDT"],
                    "timeframe": "4h",
                    "allocation": 0.5,
                },
                {
                    "name": "rsi_reversion",
                    "symbols": ["BTCUSDT"],
                    "timeframe": "1h",
                    "allocation": 0.5,
                },
            ],
        }
    )
    print_notes(config, BarStore(tmp_path))
    assert "symbols trade on different bars (BTCUSDT 1h, ETHUSDT 4h)" in capsys.readouterr().out


def test_a_key_given_twice_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    paper = tmp_path / "config" / "paper.yaml"
    paper.write_text(paper.read_text("utf-8") + "initial_cash: 500\ninitial_cash: 50000\n", "utf-8")
    assert main(["status", "paper"]) == EXIT_CONFIG
    assert "key 'initial_cash' given twice" in capsys.readouterr().err


def test_a_start_without_keys_ends_for_good(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    monkeypatch.setattr("tbot.cli.Settings", lambda: Settings(_env_file=None))  # type: ignore[call-arg]
    for name in ("TBOT_BINANCE_API_KEY", "TBOT_BINANCE_API_SECRET"):
        monkeypatch.delenv(name, raising=False)
    assert main(["live", "testnet"]) == EXIT_CONFIG  # the supervisor gives up: no retry helps
    assert "no Binance API keys: `tbot keys testnet`" in capsys.readouterr().err
    assert not (tmp_path / "testnet.sqlite").exists()  # no ledger left for nothing


def test_a_grid_typo_fails_before_any_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "base.yaml").write_text(f"start: 2020-01-01\nstrategies:\n{STRATEGY}", "utf-8")
    validation = tmp_path / "validation.yaml"
    for grid in ("{entri: [20, 30]}", "{entry: []}"):
        validation.write_text(
            f"backtest: base.yaml\nholdout_start: 2024-01-01\ngrid: {grid}\n", "utf-8"
        )
        assert main(["validate", str(validation), "--data-dir", str(tmp_path)]) == EXIT_CONFIG
    err = capsys.readouterr().err
    assert "entri" in err
    assert "grid: no values for entry" in err
    assert not (tmp_path / "trials.jsonl").exists()  # nothing ran, nothing was logged


def test_a_period_outside_the_stored_bars_is_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = BarStore(tmp_path)
    store.write(
        "BTCUSDT", Timeframe.H4, make_bars(datetime(2024, 1, 1, tzinfo=UTC), 30, Timeframe.H4)
    )
    config = tmp_path / "late.yaml"
    config.write_text(f"start: 2025-01-01\nstrategies:\n{STRATEGY}", "utf-8")
    assert main(["backtest", str(config), "--data-dir", str(tmp_path)]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "BTCUSDT 4h is stored from 2024-01-01 to 2024-01-05, before the period starts" in err
    assert not (tmp_path / "trials.jsonl").exists()


def test_output_can_be_a_folder(tmp_path: Path) -> None:
    assert output_file(None, Path("reports"), "a.html") == Path("reports") / "a.html"
    assert output_file(str(tmp_path), Path("reports"), "a.html") == tmp_path / "a.html"
    assert output_file("new/", Path("reports"), "a.html") == Path("new") / "a.html"
    assert output_file("new\\", Path("reports"), "a.html") == Path("new") / "a.html"
    assert output_file("x.html", Path("reports"), "a.html") == Path("x.html")


def test_yml_configs_have_names_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    (tmp_path / "config" / "mine.yml").write_text(
        f"ledger: mine.sqlite\nstrategies:\n{STRATEGY}", "utf-8"
    )
    assert config_arg("mine") == Path("config/mine.yml")
    assert "mine" in config_names()


def test_a_second_start_is_refused_before_it_says_how_to_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    with instance_lock(tmp_path / "paper.sqlite"):
        Ledger(tmp_path / "paper.sqlite").close()
        assert main(["paper"]) == EXIT_LEDGER
    err = capsys.readouterr().err
    assert "a session already runs on paper.sqlite" in err
    assert "stop it with Ctrl+C" not in err


def test_notify_takes_a_new_token_for_a_revoked_one(
    no_telegram: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    telegram = FakeTelegram(monkeypatch, [[message(42, "Tom", CODE)]])
    telegram.status = 401  # the token in .env was revoked
    save_settings(no_telegram, {"TBOT_TELEGRAM_TOKEN": "111:old"})
    answers = iter(["222:new"])  # a new token

    def answer(prompt: str = "") -> str:
        telegram.status = 200  # Telegram knows the new one
        return next(answers)

    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr("tbot.cli.interactive", lambda: True)
    assert main(["notify"]) == 0
    assert "TBOT_TELEGRAM_TOKEN=222:new\n" in no_telegram.read_text("utf-8")


def test_merge_keys_are_no_key_given_twice() -> None:
    text = "base: &base\n  x: 1\nother:\n  <<: *base\n  y: 2\n"
    assert load_yaml(text)["other"] == {"x": 1, "y": 2}
    with pytest.raises(yaml.YAMLError, match="key 'y' given twice"):
        load_yaml(text + "  y: 3\n")


def test_a_start_waits_out_a_brief_check_by_another_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    config = load_session_config(Path("config/paper.yaml"))
    config.ledger.write_bytes(b"")
    held = threading.Event()

    def brief_check() -> None:
        with instance_lock(config.ledger):  # what `tbot status` does
            held.set()
            time.sleep(0.5)

    thread = threading.Thread(target=brief_check)
    thread.start()
    held.wait()
    try:
        not_running(config)  # a supervised restart at that moment still starts
    finally:
        thread.join()


def test_tbot_runs_in_the_repository_wherever_it_is_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, elsewhere = tmp_path / "repo", tmp_path / "elsewhere"
    (repo / "config").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("", "utf-8")
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    seen: list[Path] = []

    def main() -> int:
        seen.append(Path.cwd())
        return 0

    monkeypatch.setattr("tbot.cli.main", main)
    monkeypatch.setattr("tbot.cli.ORIGIN", None)  # run() sets it: restored after the test
    monkeypatch.setattr("tbot.cli.ROOT", tmp_path / "installed")  # a wheel: no repository
    assert run() == 0
    monkeypatch.setattr("tbot.cli.ROOT", repo)
    assert run() == 0
    assert seen == [elsewhere, repo]


def test_two_configs_on_one_ledger_are_pointed_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    paper = tmp_path / "config" / "paper.yaml"
    (tmp_path / "config" / "mypaper.yaml").write_text(
        paper.read_text("utf-8") + "initial_cash: 500\n", "utf-8"
    )
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "config/mypaper.yaml uses paper.sqlite too" in out.replace("\\", "/")
    assert main(["doctor", "mypaper", "--offline"]) in (0, 1)
    out = capsys.readouterr().out.replace("\\", "/")
    assert "WARN ledger: config/paper.yaml uses paper.sqlite too: one book for two" in out


def test_account_without_keys_says_so_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    monkeypatch.setattr("tbot.cli.Settings", lambda: Settings(_env_file=None))  # type: ignore[call-arg]
    for name in ("TBOT_BINANCE_API_KEY", "TBOT_BINANCE_API_SECRET"):
        monkeypatch.delenv(name, raising=False)

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("no request without keys")

    monkeypatch.setattr("tbot.cli.account_text", no_network)
    assert main(["account", "testnet"]) == EXIT_CONFIG
    assert "testnet.binance.vision" in capsys.readouterr().err


def test_debug_is_on_only_when_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(args: object) -> int:
        raise RuntimeError("boom")

    monkeypatch.setitem(COMMANDS, "check", broken)
    monkeypatch.setenv("TBOT_DEBUG", "0")
    assert main(["check"]) == EXIT_ERROR
    assert "error: boom" in capsys.readouterr().err
    monkeypatch.setenv("TBOT_DEBUG", "1")
    with pytest.raises(RuntimeError, match="boom"):
        main(["check"])


def test_a_supervised_session_logs_only_problems_to_its_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    levels: list[int] = []
    monkeypatch.setattr(
        "tbot.cli.configure_logging",
        lambda log_file, console_level: levels.append(console_level),
    )
    monkeypatch.delenv("TBOT_SUPERVISED", raising=False)
    session_logging(tmp_path / "paper.jsonl")
    monkeypatch.setenv("TBOT_SUPERVISED", "1")  # the console file is never rotated
    session_logging(tmp_path / "paper.jsonl")
    assert levels == [logging.INFO, logging.WARNING]


def test_a_missing_ledger_names_the_command_that_starts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    assert main(["compare", "testnet"]) == EXIT_ERROR  # the config is fine: nothing ran
    assert "(`tbot live testnet` starts it;" in capsys.readouterr().err


def test_files_named_from_another_folder_are_found_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "mine.yaml").write_text(f"start: 2024-01-01\nstrategies:\n{STRATEGY}", "utf-8")
    monkeypatch.setattr("tbot.cli.ORIGIN", elsewhere)  # what run() records before it moves
    assert config_arg("mine.yaml") == elsewhere / "mine.yaml"
    assert config_arg("paper") == Path("config/paper.yaml")  # names stay names
    assert config_arg("config/testnet.yaml") == Path("config/testnet.yaml")  # the repository's
    assert output_file("x.html", Path("reports"), "a.html") == elsewhere / "x.html"


def test_symbols_are_written_as_binance_writes_them(capsys: pytest.CaptureFixture[str]) -> None:
    def strategy(*symbols: str) -> StrategyConfig:
        return StrategyConfig(
            name="donchian_trend", symbols=list(symbols), timeframe=Timeframe.H4, allocation=1.0
        )

    assert strategy("btc-usdt", "ETH_USDT", "SOL/USDT").symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    with pytest.raises(ValidationError, match="no Binance spot symbol"):
        strategy("BTC.USDT")
    assert main(["download", "--symbols", "BTC:USDT"]) == EXIT_CONFIG
    assert "'BTC:USDT' is no Binance spot symbol" in capsys.readouterr().err


def test_a_config_of_another_kind_names_the_command_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    assert main(["live", "paper"]) == EXIT_CONFIG
    err = capsys.readouterr().err.replace("\\", "/")
    assert "config/paper.yaml is a paper config: run it with `tbot paper`" in err
    assert main(["backtest", "testnet"]) == EXIT_CONFIG
    assert "is a testnet config: run it with `tbot live testnet`" in capsys.readouterr().err
    assert main(["status", "trend"]) == EXIT_CONFIG
    assert "is a backtest config: run it with `tbot backtest trend`" in capsys.readouterr().err
    real = f"mode: live\ninitial_cash: 1000\nledger: live.sqlite\nstrategies:\n{STRATEGY}"
    (tmp_path / "config" / "real.yaml").write_text(real, "utf-8")
    assert main(["backtest", "real"]) == EXIT_CONFIG
    assert "is a live config: run it with `tbot live real --live`" in capsys.readouterr().err
    assert main(["paper", "real"]) == EXIT_CONFIG
    assert "this is a live config; use `tbot live real --live`" in capsys.readouterr().err


def test_a_broken_config_is_named_by_what_the_command_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    # A backtest without its start: no valid paper config either, so no `tbot paper` advice.
    nostart = f"end: 2024-06-01\nstrategies:\n{STRATEGY}"
    (tmp_path / "config" / "nostart.yaml").write_text(nostart, "utf-8")
    assert main(["backtest", "nostart"]) == EXIT_CONFIG
    err = capsys.readouterr().err
    assert "paper config" not in err
    assert "start: Field required" in err
    (tmp_path / "config" / "empty.yaml").write_text("", "utf-8")
    assert main(["backtest", "empty"]) == EXIT_CONFIG
    assert "paper config" not in capsys.readouterr().err
    (tmp_path / "config" / "folder.yaml").mkdir()
    assert main(["status", "config/folder.yaml"]) == EXIT_CONFIG
    assert "is a folder, not a config file" in capsys.readouterr().err


def test_a_validation_names_its_broken_base_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    folder = tmp_path / "config"
    (folder / "base.yaml").write_text("start: 2024-01-01\nstart: 2024-02-01\n", "utf-8")
    validation = "backtest: base.yaml\nholdout_start: 2025-01-01\ngrid: {entry: [20]}\n"
    (folder / "check.yaml").write_text(validation, "utf-8")
    assert main(["validate", "check"]) == EXIT_CONFIG
    err = capsys.readouterr().err.replace("\\", "/")
    assert "config/base.yaml: not valid YAML" in err
    assert "Traceback" not in err


def test_a_monthly_timeframe_is_no_minute() -> None:
    assert parse_timeframe("4H") == Timeframe.H4
    assert parse_timeframe("1D") == Timeframe.D1
    with pytest.raises(argparse.ArgumentTypeError, match="'1M' is no timeframe"):
        parse_timeframe("1M")


def test_a_mistyped_key_gets_a_suggestion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    paper = tmp_path / "config" / "paper.yaml"
    text = paper.read_text("utf-8")
    paper.write_text(text + "initial_cahs: 500\nguard: {max_drawdown: 45}\n", "utf-8")
    assert main(["status", "paper"]) == EXIT_CONFIG
    err = capsys.readouterr().err
    assert "initial_cahs: unknown key (did you mean initial_cash?)" in err
    assert "guard.max_drawdown: Input should be less than 1 (a fraction: 0.45 means 45%)" in err
    params = STRATEGY.replace("allocation: 1.0}", "allocation: 1.0, params: {exti: 5}}")
    paper.write_text(f"ledger: paper.sqlite\nstrategies:\n{params}", "utf-8")
    assert main(["paper", "paper"]) == EXIT_CONFIG
    assert "strategies.0.params.exti: unknown key (did you mean exit?)" in capsys.readouterr().err


def test_a_paper_config_without_a_ledger_line_is_a_session_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    (tmp_path / "config" / "mini.yaml").write_text(f"strategies:\n{STRATEGY}", "utf-8")
    assert main(["status"]) == 0
    assert any(line.startswith("mini ") for line in capsys.readouterr().out.splitlines())


def test_help_is_a_command_too(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["help"]) == 0
    assert "usage: tbot" in capsys.readouterr().out
    assert main(["help", "paper"]) == 0
    assert "usage: tbot paper" in capsys.readouterr().out


def test_wrong_values_say_what_is_expected(capsys: pytest.CaptureFixture[str]) -> None:
    assert parse_timeframe("4H") == Timeframe.H4
    assert main(["download", "--timeframes", "4x"]) == EXIT_CONFIG
    assert "'4x' is no timeframe; use one of 1m, 3m" in capsys.readouterr().err
    assert main(["download", "--start", "2026-02-30"]) == EXIT_CONFIG
    assert "'2026-02-30' is no date; use YYYY-MM-DD" in capsys.readouterr().err
    assert output_file("mydash", Path("reports"), "a.html") == Path("mydash.html")
    assert build_parser().parse_args(["backtest", "x", "--html"]).html == ""


def two_streams(store: BarStore, eth_start: datetime, eth_bars: int) -> BacktestConfig:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    flat = [100.0] * 600  # 100 days of 4h bars
    store.write("BTCUSDT", Timeframe.H4, price_bars(start, Timeframe.H4, flat, flat))
    short = [100.0] * eth_bars
    store.write("ETHUSDT", Timeframe.H4, price_bars(eth_start, Timeframe.H4, short, short))
    return BacktestConfig.model_validate(
        {
            "start": "2024-01-02",
            "end": "2024-04-01",
            "strategies": [
                {
                    "name": "donchian_trend",
                    "symbols": ["BTCUSDT", "ETHUSDT"],
                    "timeframe": "4h",
                    "allocation": 1.0,
                    "params": {"entry": 5, "exit": 3},
                }
            ],
        }
    )


def test_a_stream_that_ends_early_or_starts_late_is_named_before_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = BarStore(tmp_path)
    config = two_streams(store, datetime(2024, 1, 21, tzinfo=UTC), 300)  # to 2024-03-11
    with pytest.raises(ValueError, match="ETHUSDT 4h data ends 2024-03-11 00:00, before the"):
        check_period(config, store)  # a delisted symbol would cut the whole run short
    print_notes(config.model_copy(update={"end": None}), store)
    out = capsys.readouterr().out
    assert "ETHUSDT 4h data ends 2024-03-11 00:00 (its last close)" in out
    assert "leave it out or set end:" in out
    assert "ETHUSDT 4h data starts 2024-01-21, after the period starts" in out


def test_a_week_of_missing_bars_inside_the_period_is_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = BarStore(tmp_path)
    config = two_streams(store, datetime(2024, 1, 1, tzinfo=UTC), 600)
    eth = store.read("ETHUSDT", Timeframe.H4)
    hole = (eth["open_time"] < datetime(2024, 2, 1, tzinfo=UTC)) | (
        eth["open_time"] >= datetime(2024, 2, 1, tzinfo=UTC) + timedelta(days=10)
    )
    other = BarStore(tmp_path / "holed")
    other.write("BTCUSDT", Timeframe.H4, store.read("BTCUSDT", Timeframe.H4))
    other.write("ETHUSDT", Timeframe.H4, eth.filter(hole))
    print_notes(config, other)
    assert "ETHUSDT 4h misses a week or more of bars inside the period" in capsys.readouterr().out


def test_a_holdout_after_the_stored_bars_fails_before_anything_runs(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    base = two_streams(store, datetime(2024, 1, 1, tzinfo=UTC), 600)
    late = ValidationConfig.model_validate(
        {"backtest": "base.yaml", "holdout_start": date(2024, 6, 1), "grid": {"entry": [5]}}
    )
    with pytest.raises(ValueError, match="holdout_start 2024-06-01 is after the stored"):
        check_holdout(late, base.model_copy(update={"end": None}), store)


def test_help_for_a_windows_script_says_where_it_is(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["help", "autostart"]) == 0
    assert "`tbot autostart -h` shows its help" in capsys.readouterr().out


def test_a_backtest_config_keeps_no_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    assert main(["log", "trend"]) == EXIT_CONFIG
    assert "is a backtest config: it prints, and keeps no log" in capsys.readouterr().err


def test_a_path_that_cannot_be_checked_is_no_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real = Path.exists

    def denied(self: Path, *args: Any, **kwargs: Any) -> bool:
        if "locked" in str(self):
            raise PermissionError(13, "Permission denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", denied)
    assert config_arg("locked/x.yaml") == Path("locked/x.yaml")


def test_streams_of_different_timeframes_end_apart_without_a_note(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = BarStore(tmp_path)
    start = datetime(2024, 1, 1, tzinfo=UTC)
    flat = [100.0] * 2003  # the 1h stream closes 3 h after the last 4h close
    store.write("BTCUSDT", Timeframe.H1, price_bars(start, Timeframe.H1, flat, flat))
    store.write("BTCUSDT", Timeframe.H4, price_bars(start, Timeframe.H4, flat[:500], flat[:500]))
    config = BacktestConfig.model_validate(
        {
            "start": "2024-01-10",
            "strategies": [
                {
                    "name": "donchian_trend",
                    "symbols": ["BTCUSDT"],
                    "timeframe": t,
                    "allocation": 0.5,
                }
                for t in ("1h", "4h")
            ],
        }
    )
    print_notes(config, store)  # the 1h stream closes 3 h after the last 4h close
    assert "data ends" not in capsys.readouterr().out
    store.write("BTCUSDT", Timeframe.H1, price_bars(start, Timeframe.H1, flat * 2, flat * 2))
    print_notes(config, store)  # now the 4h stream is far behind
    assert "BTCUSDT 4h data ends" in capsys.readouterr().out


def test_a_session_that_dies_outside_its_own_alerts_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    sent: list[str] = []
    monkeypatch.setattr("tbot.cli._alert", sent.append)

    def unwritable(*args: object, **kwargs: object) -> int:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr("tbot.cli.run_paper", unwritable)
    monkeypatch.setattr("tbot.cli.configure_logging", lambda *args, **kwargs: None)
    assert main(["paper", "paper"]) == EXIT_ERROR
    assert sent == ["tbot stopped: error: database or disk is full"]


def test_a_support_report_holds_no_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    key = "A1b2" * 16  # 64 characters, as a Binance key
    token = "123456789:" + "x" * 35
    monkeypatch.setenv("TBOT_BINANCE_API_KEY", key)
    monkeypatch.setenv("TBOT_TELEGRAM_TOKEN", token)
    monkeypatch.setenv("TBOT_HEARTBEAT_URL", "https://hc-ping.com/abc123def456")
    logs = tmp_path / "logs"
    logs.mkdir()
    other = "Z9y8" * 16  # a key that is in no setting: caught by its shape
    event = f"oops {key} {token} {other}"
    leaked = {"timestamp": "2026-10-13T00:00:00Z", "level": "error", "event": event}
    (logs / "paper.jsonl").write_text(json.dumps(leaked) + "\n", encoding="utf-8")
    out = tmp_path / "report.txt"
    assert main(["support", "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "TBOT_BINANCE_API_KEY" in text  # which settings are set, never their values
    assert key not in text
    assert token not in text
    assert "hc-ping.com/abc123" not in text
    assert other not in text
    assert "oops [secret] [secret] [secret]" in text
    assert "== doctor --offline" in text
    assert main(["support", "--out", str(out)]) == EXIT_CONFIG  # never over a file


def test_a_config_of_another_kind_that_does_not_load_says_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    check = "backtest: missing.yaml\nholdout_start: 2025-01-01\ngrid: {entry: [20]}\n"
    (tmp_path / "config" / "check.yaml").write_text(check, "utf-8")
    assert main(["backtest", "check"]) == EXIT_CONFIG
    err = capsys.readouterr().err
    assert "is a validation config: run it with `tbot validate check`, but it does not" in err
    assert "did you mean backtest?" not in err


def test_a_stream_short_of_its_own_bars_is_named_beside_a_coarser_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = BarStore(tmp_path)
    start = datetime(2024, 1, 1, tzinfo=UTC)
    flat = [100.0] * 2400
    store.write("BTCUSDT", Timeframe.H1, price_bars(start, Timeframe.H1, flat, flat))
    store.write("ETHUSDT", Timeframe.H1, price_bars(start, Timeframe.H1, flat[:-20], flat[:-20]))
    store.write("BTCUSDT", Timeframe.D1, price_bars(start, Timeframe.D1, flat[:100], flat[:100]))
    hourly = {"name": "donchian_trend", "symbols": ["BTCUSDT", "ETHUSDT"], "timeframe": "1h"}
    daily = {"name": "donchian_trend", "symbols": ["BTCUSDT"], "timeframe": "1d"}
    config = BacktestConfig.model_validate(
        {
            "start": "2024-01-20",
            "strategies": [{**hourly, "allocation": 0.5}, {**daily, "allocation": 0.5}],
        }
    )
    print_notes(config, store)  # ETH 1h ends 20 hours early; the 1d stream must not hide it
    assert "ETHUSDT 1h data ends" in capsys.readouterr().out


def test_a_page_named_wrongly_fails_before_the_backtest_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = BarStore(tmp_path)
    flat = [100.0] * 600
    start = datetime(2024, 1, 1, tzinfo=UTC)
    store.write("BTCUSDT", Timeframe.H4, price_bars(start, Timeframe.H4, flat, flat))
    store.write("ETHUSDT", Timeframe.H4, price_bars(start, Timeframe.H4, flat, flat))
    args = ["backtest", "donchian_trend", "--data-dir", str(tmp_path), "--html", "out.txt"]
    assert main(args) == EXIT_CONFIG
    assert "the page is HTML" in capsys.readouterr().err
    assert not (tmp_path / "trials.jsonl").exists()  # nothing ran, nothing was logged


def test_minutes_in_capitals_are_minutes() -> None:
    assert parse_timeframe("15M") == Timeframe.M15
    with pytest.raises(argparse.ArgumentTypeError):
        parse_timeframe("1M")


def test_a_backtest_on_history_never_downloaded_is_refused(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    recent = datetime(2026, 8, 7, tzinfo=UTC)  # what a session start fetched, no more
    flat = [100.0] * 400
    for symbol in ("BTCUSDT", "ETHUSDT"):
        store.write(symbol, Timeframe.H4, price_bars(recent, Timeframe.H4, flat, flat))
    config = load_config(Path("config/donchian_trend.yaml"))  # from 2018
    with pytest.raises(ValueError, match="the stored bars start 2026-08-07, long after"):
        check_period(config, store)


def test_check_names_short_gaps_as_normal_and_missing_defaults_as_not_downloaded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = BarStore(tmp_path)
    bars = make_bars(datetime(2024, 1, 1, tzinfo=UTC), 100, Timeframe.H1)
    store.write(
        "BTCUSDT", Timeframe.H1, bars.filter(~pl.col("open_time").is_in(bars["open_time"][10:13]))
    )
    assert main(["check", "--data-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "gaps: 1 (3 missing bars); 1 shorter than a day: exchange pauses, normal" in out
    assert "ETHUSDT 4h: not downloaded" in out
    assert main(["check", "--data-dir", str(tmp_path), "--symbols", "ETHUSDT"]) == 1


def test_a_never_started_live_config_does_not_point_at_starting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    real = f"mode: live\ninitial_cash: 1000\nledger: live.sqlite\nstrategies:\n{STRATEGY}"
    (tmp_path / "config" / "real.yaml").write_text(real, "utf-8")
    assert main(["status", "real"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "real money: read README, Before real money, then `tbot doctor real`" in err
    assert "--live" not in err


def test_testnet_keys_of_their_own_come_first_for_testnet() -> None:
    both = Settings(  # type: ignore[call-arg]
        _env_file=None,
        binance_api_key="live-key",
        binance_api_secret="live-secret",
        binance_testnet_api_key="test-key",
        binance_testnet_api_secret="test-secret",
    )
    assert both.api_keys("testnet") == ("test-key", "test-secret")
    assert both.api_keys("live") == ("live-key", "live-secret")
    main_only = Settings(  # type: ignore[call-arg]
        _env_file=None, binance_api_key="k", binance_api_secret="s"
    )
    assert main_only.api_keys("testnet") == ("k", "s")
    assert Settings(_env_file=None).api_keys("live") is None  # type: ignore[call-arg]


def test_settings_lose_the_spaces_a_quoted_value_keeps() -> None:
    settings = Settings(_env_file=None, binance_api_key="  key\t")  # type: ignore[call-arg]
    assert settings.binance_api_key == "key"
