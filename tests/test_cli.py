import asyncio
import io
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from binance_fake import make_bars
from tbot import __version__
from tbot.cli import (
    EXIT_CONFIG,
    EXIT_ERROR,
    EXIT_LEDGER,
    announce,
    build_parser,
    config_arg,
    load_session_config,
    main,
    parse_symbol,
    pick_config,
    write_dashboard,
)
from tbot.core.timeframe import Timeframe
from tbot.data.store import BarStore
from tbot.live.config import save_settings
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
    assert capsys.readouterr().out == "no session is running\n"
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
        "resumed (was halted: drawdown 50%); a running bot trades again from its next bar",
    ]


def test_status_without_a_config_shows_every_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_configs(tmp_path)
    assert main(["status"]) == 0
    assert capsys.readouterr().out.splitlines()[:3] == [
        "config   state",
        "paper    not started",
        "testnet  not started",
    ]
    with Ledger(tmp_path / "paper.sqlite") as ledger:
        ledger.add_equity(EquityPoint(datetime(2026, 10, 1, tzinfo=UTC), 10_250.0, 10_250.0, 0.0))
    assert main(["status"]) == 0
    row = capsys.readouterr().out.splitlines()[1].split()
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


def message(chat: int, name: str) -> dict[str, Any]:
    return {"update_id": chat, "message": {"chat": {"id": chat, "first_name": name}}}


@pytest.fixture
def no_telegram(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    for name in ("TBOT_TELEGRAM_TOKEN", "TBOT_TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env.example").write_bytes(
        b"# Telegram\nTBOT_TELEGRAM_TOKEN=\nTBOT_TELEGRAM_CHAT_ID=\nTBOT_HEARTBEAT_URL=\n"
    )
    return tmp_path / ".env"


def test_notify_guides_a_first_setup_and_saves_it(
    no_telegram: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    telegram = FakeTelegram(monkeypatch, [[], [message(42, "Tom")]])
    answers = iter(["  123:abc  "])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr("tbot.cli.interactive", lambda: True)
    assert main(["notify"]) == 0
    assert no_telegram.read_bytes() == (
        b"# Telegram\nTBOT_TELEGRAM_TOKEN=123:abc\nTBOT_TELEGRAM_CHAT_ID=42\nTBOT_HEARTBEAT_URL=\n"
    )
    assert telegram.sent == [("42", "tbot: test alert. Alerts from the bot arrive in this chat.")]
    out = capsys.readouterr().out
    assert "Now send any message to @tbot_test_bot in Telegram" in out  # it waited for one
    assert "saved chat 42 in .env" in out
    assert main(["notify"]) == 0  # set up: only the test message
    assert len(telegram.sent) == 2


def test_notify_without_a_terminal_or_with_a_wrong_token(
    no_telegram: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    telegram = FakeTelegram(monkeypatch, [[]])
    assert main(["notify"]) == EXIT_ERROR  # nobody to ask for the token
    assert "set TBOT_TELEGRAM_TOKEN (from @BotFather) in .env" in capsys.readouterr().out
    monkeypatch.setenv("TBOT_TELEGRAM_TOKEN", "123:abc")
    assert main(["notify"]) == EXIT_ERROR
    assert "no message to @tbot_test_bot yet" in capsys.readouterr().out
    telegram.inbox = [[message(1, "Tom"), message(2, "Group")]]
    assert main(["notify"]) == EXIT_ERROR  # two chats and nobody to choose
    out = capsys.readouterr().out
    assert "1. chat 1 Tom\n2. chat 2 Group\n" in out
    assert not no_telegram.exists()
    monkeypatch.setattr("tbot.cli.interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "2")
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
