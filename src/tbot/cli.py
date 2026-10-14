"""Command line entry point."""

import argparse
import asyncio
import difflib
import getpass
import hashlib
import io
import logging
import os
import platform
import re
import secrets
import sys
import webbrowser
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import httpx
import yaml
from pydantic import BaseModel, ValidationError

from tbot import __version__
from tbot.backtest.attribution import format_attribution, run_attribution
from tbot.backtest.config import BacktestConfig, load_config
from tbot.backtest.metrics import compute_metrics
from tbot.backtest.report import format_metrics
from tbot.backtest.runner import data_ends, history_bars, run_backtest
from tbot.core.config import TradingConfig, load_yaml, normalize_symbol
from tbot.core.text import clock_text
from tbot.core.timeframe import Timeframe
from tbot.data.downloader import sync
from tbot.data.http import describe_error, retry_after
from tbot.data.quality import LONG_GAP, QualityReport, check_bars
from tbot.data.store import BarStore
from tbot.live.backup import backup_dir
from tbot.live.clock import CLOSE_GRACE, ServerClock
from tbot.live.commands import RANK_PAIRING, PollTurn, command_of
from tbot.live.compare import compare_session, comparison_text
from tbot.live.config import LiveConfig, PaperConfig, SessionConfig, Settings, save_settings
from tbot.live.doctor import Check, checks_text, key_checks, run_checks, works_on_testnet
from tbot.live.ledger import Ledger, LedgerUnavailable
from tbot.live.overview import broken_row, overview_text, session_row
from tbot.live.runner import (
    BUDGET_META,
    KEYS_MISSING,
    STOP_REQUEST_SECONDS,
    SUPERVISED_META,
    AlreadyRunning,
    LedgerModeError,
    StartRefused,
    account_text,
    halt_book,
    halt_path,
    instance_lock,
    is_running,
    load_guard,
    next_close,
    restore_portfolio,
    resume,
    run_live,
    run_paper,
    status_text,
    stop_path,
    supervised,
)
from tbot.monitoring.dashboard import DashboardData, from_backtest, from_ledger, render
from tbot.monitoring.logging import configure_logging, configure_quiet
from tbot.monitoring.logview import LEVELS, follow, tail
from tbot.monitoring.telegram import Telegram
from tbot.portfolio.allocation import build_slots
from tbot.research.report import format_report
from tbot.research.trials import TrialLog, make_record
from tbot.research.validate import ValidationConfig, load_validation_config, run_validation

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT"]
# Exit codes: scripts/run_bot.ps1 starts the bot again after 1 and gives up on 3 and 4.
EXIT_ERROR = 1  # a crash, a failed start, or an error worth retrying
EXIT_LEDGER = 3  # the ledger cannot be used: another process has it, it is damaged, no SQLite
EXIT_CONFIG = 4  # the command or the config is wrong: retrying cannot help


class ConfigError(SystemExit):
    """A wrong command or config; main() turns it into one line and EXIT_CONFIG."""


class NothingYet(Exception):
    """Nothing has run with a config yet: no ledger to read. Not a wrong config."""


def known_keys() -> list[str]:
    """Every setting name of tbot's config models, for "did you mean" hints."""
    names: set[str] = set()
    models: list[type[BaseModel]] = [BaseModel]
    while models:
        model = models.pop()
        models.extend(model.__subclasses__())
        if model.__module__.startswith("tbot."):
            names.update(model.model_fields)
    return sorted(names)


def brief(exc: BaseException, prefix: str = "") -> str:
    """One line for an error a user can act on."""
    if isinstance(exc, ValidationError):
        return "; ".join(_problem(error, prefix) for error in exc.errors())
    if isinstance(exc, UnicodeDecodeError):
        return f"a file is not UTF-8 text; save .env and configs as UTF-8 ({exc})"
    if isinstance(exc, httpx.HTTPStatusError):
        answer = exc.response.text[:200]
        return f"HTTP {exc.response.status_code} from {exc.request.url.host}: {answer}"
    if isinstance(exc, httpx.HTTPError):
        return f"network error: {exc!r}"
    return str(exc) or repr(exc)


def _problem(error: Any, prefix: str) -> str:
    where = prefix + ".".join(str(part) for part in error["loc"]) or "config"
    if error["type"] == "extra_forbidden":
        key = str(error["loc"][-1])
        close = [k for k in difflib.get_close_matches(key, known_keys(), n=2) if k != key]
        return f"{where}: unknown key" + (f" (did you mean {close[0]}?)" if close else "")
    message = str(error["msg"])
    limit = (error.get("ctx") or {}).get("lt", (error.get("ctx") or {}).get("le"))
    if limit == 1:
        message += " (a fraction: 0.45 means 45%)"
    return f"{where}: {message}"


DEFAULT_TIMEFRAMES = [Timeframe.H1, Timeframe.H4]
PAST_RESULTS = (
    "Past results, not a forecast: settings picked on this data look better here than they "
    "will trade (`tbot validate` tests them on data they were not picked on)."
)
DEFAULT_START = "2017-08-01"
MAX_LISTED = 10
CONFIG_DIR = Path("config")
CONFIG_SUFFIXES = (".yaml", ".yml")
CHAT_WAIT_SECONDS = 120  # `tbot notify` waits this long for the code to reach the bot
TURN_WAIT_SECONDS = 90  # and this long for a running session to stop reading messages
ROOT = Path(__file__).resolve().parents[2]  # the repository, when tbot runs from a clone
DATA_HELP = "bar store and trial log (default: data)"
SESSION_DATA_HELP = "bar store (default: data); the ledger is set in the config"
ORIGIN: Path | None = None  # where the command was typed, when run() changed folders
SCRIPTS = ("setup", "update", "autostart")  # tbot.cmd runs these PowerShell scripts


def typed_path(value: str) -> Path:
    """A file the user named: relative to the folder the command was typed in."""
    path = Path(value)
    return ORIGIN / path if ORIGIN is not None and not path.is_absolute() else path


def exists(path: Path) -> bool:
    """Whether the path is there; one that cannot be checked counts as not, and reading
    it later says why."""
    try:
        return path.exists()
    except OSError:
        return False


def input_path(value: str) -> Path:
    """A file to read: where the command was typed, else in the repository."""
    typed = typed_path(value)
    return typed if exists(typed) or not exists(Path(value)) else Path(value)


def config_arg(value: str) -> Path:
    """A config file, or the name of one in config/: `paper` is config/paper.yaml."""
    path = input_path(value)
    if not exists(path) and not path.suffix and Path(value).name == value:
        for suffix in CONFIG_SUFFIXES:
            named = CONFIG_DIR / f"{value}{suffix}"
            if named.is_file():
                return named
    return path


def config_files() -> list[Path]:
    return sorted(path for suffix in CONFIG_SUFFIXES for path in CONFIG_DIR.glob(f"*{suffix}"))


def config_names() -> list[str]:
    return sorted(path.stem for path in config_files())


def ledger_sharers(path: Path, config: SessionConfig) -> list[Path]:
    """Other session configs in config/ that name this config's ledger: one book for two."""
    others = []
    for other in config_files():
        if other.resolve() == path.resolve():
            continue
        try:
            raw = read_yaml(other)
            if kind_of(raw) in ("paper", "live") and (
                load_session_config(other).ledger.resolve() == config.ledger.resolve()
            ):
                others.append(other)
        except ConfigError:
            continue
    return others


def sharing_note(path: Path, config: SessionConfig) -> str:
    others = ledger_sharers(path, config)
    if not others:
        return ""
    names = ", ".join(str(other) for other in others)
    verb = "uses" if len(others) == 1 else "use"
    return f"{names} {verb} {config.ledger} too: one book for two configs (give each a ledger)"


def short_name(path: Path) -> str:
    """What finds this config again on the command line: `paper` for config/paper.yaml."""
    return path.stem if config_arg(path.stem).resolve() == path.resolve() else str(path)


def parse_symbol(value: str) -> str:
    try:
        return normalize_symbol(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def parse_date(value: str) -> datetime:
    try:
        return datetime.combine(date.fromisoformat(value), time(), tzinfo=UTC)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is no date; use YYYY-MM-DD") from None


def parse_timeframe(value: str) -> Timeframe:
    # 4H, 1D, or 15M as well, but never 1M: that is Binance's month, not a minute.
    unit = value[-1:]
    lower = unit in ("H", "D") or (unit == "M" and value[:-1] != "1")
    for candidate in (value, value[:-1] + unit.lower() if lower else value):
        try:
            return Timeframe(candidate)
        except ValueError:
            continue
    names = ", ".join(t.value for t in Timeframe)
    raise argparse.ArgumentTypeError(f"{value!r} is no timeframe; use one of {names}")


def fmt(moment: datetime | None) -> str:
    return f"{moment:%Y-%m-%d %H:%M}" if moment else "-"


KIND_USE = {
    "validation": "a validation config: run it with `tbot validate {name}`",
    "backtest": "a backtest config: run it with `tbot backtest {name}`",
    "live": "a testnet or live config: run it with `tbot live {name}`",
    "paper": "a paper config: run it with `tbot paper {name}`",
}
KIND_LOADERS: dict[str, Callable[[Path], object]] = {}  # filled below the loaders


def live_command(name: str, config: SessionConfig | dict[str, Any]) -> str:
    """The command that starts this session: a real-money one needs --live."""
    mode = config.get("mode") if isinstance(config, dict) else getattr(config, "mode", None)
    if mode == "live":
        return f"tbot live {name} --live"
    if mode:
        return f"tbot live {name}"
    return "tbot paper" if name == "paper" else f"tbot paper {name}"  # paper is the default


def kind_of(raw: dict[str, Any]) -> str:
    """validation, backtest, live (testnet too), or paper: what a config file is for."""
    if "backtest" in raw or "grid" in raw:
        return "validation"
    if "start" in raw:
        return "backtest"
    return "live" if "mode" in raw else "paper"


def read_yaml(path: Path) -> dict[str, Any]:
    try:
        if path.is_dir():
            raise ConfigError(f"{path} is a folder, not a config file")
    except OSError:
        pass  # reading it says why
    try:
        raw = load_yaml(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        names = ", ".join(config_names()) or "none"
        raise ConfigError(f"{path}: no such config (names in {CONFIG_DIR}/: {names})") from None
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read ({exc.strerror})") from None
    except UnicodeDecodeError:
        raise ConfigError(f"{path}: not UTF-8 text; save it as UTF-8") from None
    except (yaml.YAMLError, ValueError) as exc:  # ValueError: a date such as 2024-02-30
        raise ConfigError(f"{path}: not valid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping of settings")
    return raw


def load[T](path: Path, loader: Callable[[Path], T], kinds: tuple[str, ...]) -> T:
    """Run a config loader; a bad file ends the command with a readable message, and a
    config of another kind with the command that takes it."""
    raw = read_yaml(path)
    try:
        return loader(path)
    except (ValidationError, ValueError) as exc:
        kind = kind_of(raw)
        if kind not in kinds:
            name = short_name(path)
            use = KIND_USE[kind].format(name=name)
            if kind in ("live", "paper"):
                what = raw.get("mode") or "paper"
                use = f"a {what} config: run it with `{live_command(name, raw)}`"
            failure = load_failure(path, kind)
            if failure is None:
                raise ConfigError(f"{path} is {use}") from None
            if kind != "paper":  # a paper config is only what is left: no sure sign
                raise ConfigError(
                    f"{path} is {use}, but it does not load as one: {brief(failure)}"
                ) from None
        raise ConfigError(f"{path}: {brief(exc)}") from None
    except OSError as exc:  # a file the config points at, such as a validation's backtest
        raise ConfigError(f"{path}: {exc}") from None


def load_failure(path: Path, kind: str) -> Exception | None:
    """Why the file is no valid config of this kind, or None when it is one."""
    try:
        KIND_LOADERS[kind](path)
    except (ValidationError, ValueError, OSError) as exc:
        return exc
    return None


def check_strategies(path: Path, config: TradingConfig) -> None:
    """Strategy names and params are checked when slots are built; do it before starting."""
    for index, strategy in enumerate(config.strategies):
        try:
            build_slots([strategy])
        except (ValidationError, ValueError) as exc:
            where = f"strategies.{index}.params."
            raise ConfigError(f"{path}: {strategy.name}: {brief(exc, where)}") from None


def load_session_config(path: Path) -> SessionConfig:
    """A config with `mode` is a live config; anything else is paper."""

    def parse(path: Path) -> SessionConfig:
        raw = read_yaml(path)
        if "mode" in raw:
            return LiveConfig.model_validate(raw)
        return PaperConfig.model_validate(raw)

    return load(path, parse, ("paper", "live"))


def load_live_config(path: Path) -> LiveConfig:
    return load(path, lambda path: LiveConfig.model_validate(read_yaml(path)), ("live",))


KIND_LOADERS.update(
    validation=load_validation_config,
    backtest=load_config,
    live=lambda path: LiveConfig.model_validate(read_yaml(path)),
    paper=lambda path: PaperConfig.model_validate(read_yaml(path)),
)


@dataclass(frozen=True)
class Broken:
    """A session config in config/ that does not load, and the ledger it names if any."""

    path: Path
    problem: str
    ledger: Path | None


def scan_configs() -> tuple[list[tuple[Path, SessionConfig]], list[Broken]]:
    """The paper, testnet, and live configs in config/, one per ledger, paper first.

    Configs that look like sessions but do not load come back apart, so a session still
    running on one can be shown and stopped.
    """
    order = {"paper": 0, "testnet": 1, "live": 2}
    found: dict[Path, tuple[Path, SessionConfig]] = {}
    broken: list[Broken] = []
    for path in sorted(config_files(), key=lambda p: (order.get(p.stem, 3), p.stem)):
        try:
            raw = read_yaml(path)
        except ConfigError as exc:
            broken.append(Broken(path, str(exc.code), None))
            continue
        if kind_of(raw) not in ("paper", "live"):
            continue  # a backtest or validation config
        try:
            config = load_session_config(path)
        except ConfigError as exc:
            named = raw.get("ledger")
            broken.append(
                Broken(path, str(exc.code), Path(named) if isinstance(named, str) else None)
            )
            continue
        found.setdefault(config.ledger.resolve(), (path, config))
    return list(found.values()), broken


def session_configs(*, live: bool = False) -> list[tuple[Path, SessionConfig]]:
    sessions, _ = scan_configs()
    return [(path, c) for path, c in sessions if not live or isinstance(c, LiveConfig)]


def running_configs() -> list[Path]:
    """Configs whose session runs, including one whose file no longer loads."""
    sessions, broken = scan_configs()
    ledgers = [(path, config.ledger) for path, config in sessions]
    ledgers += [(b.path, b.ledger) for b in broken if b.ledger is not None]
    return [path for path, ledger in ledgers if ledger.is_file() and is_running(ledger)]


def stop_ledger(path: Path) -> Path:
    """The ledger a stop request goes next to; a config that no longer loads still names it."""
    try:
        return load_session_config(path).ledger
    except ConfigError:
        named = read_yaml(path).get("ledger")
        if isinstance(named, str):
            return Path(named)
        raise


def pick_config(command: str, *, live: bool = False) -> Path:
    """The session a command means when none is named.

    The running session, else the only one that has run, else paper (or the only config
    there is); never a guess between two.
    """
    sessions = session_configs(live=live)
    started = [(path, config) for path, config in sessions if config.ledger.is_file()]
    running = [path for path, config in started if is_running(config.ledger)]
    paper = [path for path, _ in sessions if path.stem == "paper"]
    for group, what in (
        (running, "running"),
        ([path for path, _ in started], "with a ledger"),
        (paper or [path for path, _ in sessions], "in config/"),
    ):
        if len(group) == 1:
            return picked(group[0])
        if group:
            raise ConfigError(which(command, group, what))
    raise ConfigError(which(command, [], "in config/"))


def picked(path: Path) -> Path:
    print(f"using {path}")
    return path


def which(command: str, paths: list[Path], what: str) -> str:
    names = ", ".join(path.stem for path in paths)
    example = paths[0].stem if paths else "<config>"
    return f"name the config ({what}: {names or 'none'}), for example: tbot {command} {example}"


CONFIG_HELP = "a config file, or a name from config/ such as donchian_voltarget"
SESSION_HELP = "a config file or a name from config/ ({}); default: {}"
ANY_SESSION = "the running session, else the only one that has run, else paper"
EPILOG = """\
start here:
  tbot doctor         check the setup before a session
  tbot notify         set up Telegram alerts, guided
  tbot keys testnet   enter Binance API keys, checked before they are saved
  tbot paper          paper trade with config/paper.yaml until stopped
  tbot status         every session at a glance
  tbot log -f         follow what a session does
  tbot stop           stop the running session after the event in progress
  tbot halt live      the panic button: sell what the bot holds and stop its trading
  tbot support        a report to share when asking for help

A config is a file or a name from config/: `tbot status testnet` reads config/testnet.yaml.
On Windows, tbot.cmd also runs `tbot setup`, `tbot update`, and `tbot autostart <config>`.
`tbot <command> -h` explains a command."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tbot",
        description="Multi-strategy crypto trading bot for Binance spot.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", title="commands", metavar="<command>")

    def command(name: str, text: str) -> argparse.ArgumentParser:
        return commands.add_parser(name, help=text, description=text[0].upper() + text[1:] + ".")

    def session(
        name: str, text: str, default: str = ANY_SESSION, names: str = "paper, testnet, live"
    ) -> argparse.ArgumentParser:
        parser = command(name, text)
        parser.add_argument(
            "config", nargs="?", type=config_arg, help=SESSION_HELP.format(names, default)
        )
        return parser

    status = session(
        "status",
        "every session at a glance; with a config, that one in detail",
        "every session",
    )
    status.add_argument("--data-dir", type=typed_path, default=Path("data"), help=SESSION_DATA_HELP)

    paper = command("paper", "paper trade on live Binance data until stopped")
    paper.add_argument(
        "config", nargs="?", type=config_arg, default="paper", help="default: config/paper.yaml"
    )
    paper.add_argument("--data-dir", type=typed_path, default=Path("data"), help=SESSION_DATA_HELP)
    paper.add_argument("--log-file", type=typed_path, help="default: logs/<config name>.jsonl")

    live = command("live", "trade on Binance testnet or live until stopped")
    live.add_argument("config", type=config_arg, help="testnet, live, or a config file")
    live.add_argument("--live", action="store_true", help="required when the config mode is live")
    live.add_argument("--data-dir", type=typed_path, default=Path("data"), help=SESSION_DATA_HELP)
    live.add_argument("--log-file", type=typed_path, help="default: logs/<config name>.jsonl")

    stop_cmd = session(
        "stop", "ask a running session to stop after the event in progress", "the running one"
    )
    stop_cmd.add_argument(
        "--timeout", type=float, default=90.0, help="seconds to wait (default: %(default)s)"
    )
    stop_cmd.add_argument(
        "--cancel", action="store_true", help="withdraw a request no session has taken yet"
    )

    log_cmd = session("log", "show a session's log, readable; -f keeps following it")
    log_cmd.add_argument("-n", "--lines", type=int, default=30, help="default: %(default)s")
    log_cmd.add_argument("-f", "--follow", action="store_true", help="print new lines until Ctrl+C")
    log_cmd.add_argument(
        "--level", choices=LEVELS, default="info", help="lowest level shown (default: info)"
    )
    log_cmd.add_argument("--file", type=input_path, help="a log file to read instead, such as a .1")

    doctor = session("doctor", "check keys, alerts, network, and the ledger before a session")
    doctor.add_argument("--data-dir", type=typed_path, default=Path("data"), help=SESSION_DATA_HELP)
    doctor.add_argument(
        "--offline",
        action="store_true",
        help="skip the network checks (Binance, keys, heartbeat ping); the bot retries those",
    )

    notify = command("notify", "set up Telegram alerts (guided) and send a test message")
    notify.add_argument(
        "--chat", action="store_true", help="pick the chat again, such as a group or another phone"
    )

    keys = command("keys", "enter Binance API keys, guided: checked before they go into .env")
    keys.add_argument("config", type=config_arg, help="testnet, live, or a config file")

    dashboard = session("dashboard", "write an HTML dashboard of a session and open it")
    dashboard.add_argument(
        "--data-dir", type=typed_path, default=Path("data"), help=SESSION_DATA_HELP
    )
    dashboard.add_argument("--out", default=None, help="a file or folder (default: reports/)")
    dashboard.add_argument("--no-open", action="store_true", help="do not open it in a browser")

    compare = session("compare", "compare a session with a backtest of the same period")
    compare.add_argument(
        "--data-dir", type=typed_path, default=Path("data"), help=SESSION_DATA_HELP
    )

    backup = session("backup", "copy a session ledger, also while it runs")
    backup.add_argument(
        "--out", default=None, help="a file or folder (default: backups/ by the ledger)"
    )

    halt = session(
        "halt",
        "the panic button: sell what the bot holds and stop its trading until `tbot resume`",
        "the running one (a real-money session must be named)",
    )
    halt.add_argument(
        "--timeout", type=float, default=120.0, help="seconds to wait (default: %(default)s)"
    )
    session("resume", "clear the kill switch of a halted session", "the halted one")
    support = command("support", "write a report to share when asking for help (no keys in it)")
    support.add_argument("--out", default=None, help="a file or folder (default: reports/)")
    support.add_argument(
        "--data-dir", type=typed_path, default=Path("data"), help=SESSION_DATA_HELP
    )
    session(
        "account",
        "show exchange balances and open orders (testnet or live)",
        "the running one, else the only one that has run",
        "testnet, live",
    )

    download = command("download", "download closed bars from Binance")
    download.add_argument(
        "--start",
        type=parse_date,
        default=parse_date(DEFAULT_START),
        help=f"first day, YYYY-MM-DD (default {DEFAULT_START}); earlier history is backfilled",
    )
    check = command("check", "check stored bars")
    for parser_ in (download, check):
        parser_.add_argument(
            "--symbols",
            nargs="+",
            type=parse_symbol,
            default=DEFAULT_SYMBOLS,
            help="e.g. BTCUSDT ETH/USDT (default: %(default)s)",
        )
        parser_.add_argument(
            "--timeframes",
            nargs="+",
            type=parse_timeframe,
            default=DEFAULT_TIMEFRAMES,
            help="e.g. 1h 4h 1d (default: 1h 4h)",
        )
        parser_.add_argument(
            "--data-dir", type=typed_path, default=Path("data"), help="bar store (default: data)"
        )

    backtest = command("backtest", "run a backtest from a YAML config")
    backtest.add_argument("config", type=config_arg, help=CONFIG_HELP)
    backtest.add_argument("--data-dir", type=typed_path, default=Path("data"), help=DATA_HELP)
    backtest.add_argument(
        "--attribution", action="store_true", help="also run each strategy alone and compare"
    )
    backtest.add_argument(
        "--html",
        nargs="?",
        const="",
        default=None,
        help="write an HTML dashboard: to this file or folder, or reports/ when left out",
    )
    backtest.add_argument("--no-open", action="store_true", help="do not open the HTML file")

    validate = command("validate", "run the validation pipeline")
    validate.add_argument(
        "config",
        type=config_arg,
        help="a validation config, or its name from config/ such as donchian_voltarget_validation",
    )
    validate.add_argument("--data-dir", type=typed_path, default=Path("data"), help=DATA_HELP)
    validate.add_argument(
        "--workers",
        type=int,
        default=os.cpu_count() or 1,
        help="backtests run at once (default: the number of CPUs, %(default)s)",
    )
    return parser


def run_download(args: argparse.Namespace) -> int:
    store = BarStore(args.data_dir)
    with httpx.Client(timeout=30.0) as client:
        # A fast local clock would store a bar still open; the offset is corrected, quietly.
        clock = ServerClock(client, warn_offset=float("inf"))
        clock.sync()
        now = clock.now() - CLOSE_GRACE  # the clock may still run a little ahead
        failed = 0
        for symbol in args.symbols:
            for timeframe in args.timeframes:
                try:
                    result = sync(store, client, symbol, timeframe, args.start, now, progress=print)
                except (httpx.HTTPError, LookupError, ValueError, OSError) as exc:
                    print(f"{symbol} {timeframe}: FAILED, {brief(exc)}", file=sys.stderr)
                    wait = retry_after(exc)
                    if wait is not None:  # more requests now could get the IP banned
                        print(f"rate limited: run it again in {wait:.0f} s", file=sys.stderr)
                        return EXIT_ERROR
                    failed += 1
                    continue  # the other streams still download
                print(
                    f"{symbol} {timeframe}: {result.total_bars} bars stored, "
                    f"{fmt(result.first)} -> {fmt(result.last)}"
                )
                if result.dropped:
                    print(f"  dropped {result.dropped} misaligned bars")
    return EXIT_ERROR if failed else 0


def print_report(symbol: str, timeframe: Timeframe, report: QualityReport, store: BarStore) -> None:
    status = "OK" if report.ok else "FAIL"
    print(
        f"{symbol} {timeframe}: {status}, {report.bars} bars, "
        f"{fmt(report.first)} -> {fmt(report.last)}"
    )
    errors = {
        "duplicates": report.duplicates,
        "misaligned": report.misaligned,
        "incomplete": report.incomplete,
        "invalid values": report.invalid_values,
    }
    for name, count in errors.items():
        if count:
            print(f"  error: {count} {name}")
    if report.long_gaps:
        directory = store.directory(symbol, timeframe)
        hint = store.download_hint([(symbol, timeframe)])
        longest = max(report.long_gaps, key=lambda gap: gap.length)
        print(
            f"  error: {len(report.long_gaps)} gaps of a week or more, the longest "
            f"{fmt(longest.start)} .. {fmt(longest.end)}: data is missing; delete "
            f"{directory} and run {hint} again"
        )
    if report.gaps:
        days = [gap for gap in report.gaps if gap.length >= timedelta(days=1)]
        short = len(report.gaps) - len(days)
        print(
            f"  gaps: {len(report.gaps)} ({report.missing_bars} missing bars)"
            + (f"; {short} shorter than a day: exchange pauses, normal" if short else "")
        )
        for gap in days[:MAX_LISTED]:
            print(f"    {fmt(gap.start)} .. {fmt(gap.end)} ({gap.missing})")
        if len(days) > MAX_LISTED:
            print(f"    and {len(days) - MAX_LISTED} more of a day or longer")
    if report.zero_volume:
        print(f"  zero volume bars: {report.zero_volume}")
    if report.large_moves:
        listed = ", ".join(fmt(moment) for moment in report.large_moves[:MAX_LISTED])
        print(f"  large moves: {len(report.large_moves)} ({listed})")


def run_check(args: argparse.Namespace) -> int:
    store = BarStore(args.data_dir)
    # A minute of slack: this clock may run behind Binance's, which the download went by.
    now = datetime.now(UTC) + timedelta(minutes=1)
    failed = False
    named = args.symbols is not DEFAULT_SYMBOLS or args.timeframes is not DEFAULT_TIMEFRAMES
    for symbol in args.symbols:
        for timeframe in args.timeframes:
            bars = store.read(symbol, timeframe)
            if bars.is_empty():
                hint = store.download_hint([(symbol, timeframe)])
                if named:  # asked for by name: missing is a failure
                    print(f"{symbol} {timeframe}: FAIL, no data ({hint} fetches it)")
                    failed = True
                else:  # a default stream that no config may need
                    print(f"{symbol} {timeframe}: not downloaded ({hint} fetches it)")
                continue
            report = check_bars(bars, timeframe, now)
            print_report(symbol, timeframe, report, store)
            failed = failed or not report.ok
    return 1 if failed else 0


def trial_log(data_dir: Path) -> TrialLog:
    return TrialLog(data_dir / "trials.jsonl")


def print_notes(config: BacktestConfig, store: BarStore) -> None:
    """What a backtest of this config cannot show, said before it runs."""
    ends = data_ends(config, store)
    stop = min(max(ends.values()), utc_day(config.end)) if config.end and ends else None
    last = stop or max(ends.values(), default=None)
    # A stream that misses a bar of its own ends early; a coarser one closing up to one of
    # its bars before a finer one does not.
    short = {key: end for key, end in ends.items() if last and end + key[1].delta <= last}
    if short:
        (symbol, timeframe), first = min(short.items(), key=lambda item: item[1])
        print(
            f"Note: {symbol} {timeframe} data ends {first:%Y-%m-%d %H:%M} (its last close); "
            f"every stream stops there. {store.download_hint([(symbol, timeframe)])} brings it "
            "up to date; a symbol Binance no longer lists stays short: leave it out or set end:"
        )
    start = utc_day(config.start)
    for (symbol, timeframe), stream_end in sorted(ends.items()):
        first_open = store.first_open_time(symbol, timeframe)
        if first_open is not None and first_open > start + timedelta(days=1):
            print(
                f"Note: {symbol} {timeframe} data starts {first_open:%Y-%m-%d}, after the period "
                "starts: its share of the capital waits in cash until then"
            )
        bars = store.read(symbol, timeframe, start, stop or stream_end)
        gaps = bars["open_time"].diff().drop_nulls() if bars.height > 1 else None
        longest = gaps.max() if gaps is not None else None
        if isinstance(longest, timedelta) and longest - timeframe.delta >= LONG_GAP:
            print(
                f"Note: {symbol} {timeframe} misses a week or more of bars inside the period; "
                "results there are not real (`tbot check` shows where)"
            )
    finest: dict[str, Timeframe] = {}
    for strategy in config.strategies:
        for symbol in strategy.symbols:
            known = finest.get(symbol, strategy.timeframe)
            finest[symbol] = min(known, strategy.timeframe, key=lambda t: t.millis)
    if len(set(finest.values())) > 1:
        print(
            "Note: symbols trade on different bars ("
            + ", ".join(f"{symbol} {tf}" for symbol, tf in sorted(finest.items()))
            + "); a buy paid for by another symbol's sale fills when that sale has, a few bars "
            "later than in paper and live trading"
        )


def check_period(config: BacktestConfig, store: BarStore) -> None:
    """A period outside the stored bars would run on nothing; say what is stored."""
    for key in history_bars(build_slots(config.strategies), config.risk):
        first, last = store.first_open_time(*key), store.last_open_time(*key)
        if first is None or last is None:
            continue  # the backtest names the missing stream
        stored = f"{key[0]} {key[1]} is stored from {first:%Y-%m-%d} to {last:%Y-%m-%d}"
        if last + key[1].delta <= utc_day(config.start):
            hint = store.download_hint([key])
            raise ValueError(f"{stored}, before the period starts: run {hint}")
        if config.end is not None and utc_day(config.end) <= first:
            raise ValueError(f"{stored}, after the period ends: no download goes back further")
    keys = list(history_bars(build_slots(config.strategies), config.risk))
    firsts = [first for key in keys if (first := store.first_open_time(*key)) is not None]
    begin = utc_day(config.start)
    span = (utc_day(config.end) if config.end else datetime.now(UTC)) - begin
    if firsts and min(firsts) - begin > max(timedelta(days=30), span / 10):
        # Not one stream reaches back to the start: the history was never downloaded,
        # and a short run would pass for the whole period.
        raise ValueError(
            f"the stored bars start {min(firsts):%Y-%m-%d}, long after the period's start "
            f"{config.start}: run {store.download_hint(keys)}, or set start: later"
        )
    ends = data_ends(config, store)
    if config.end is not None and ends:
        (symbol, timeframe), short = min(ends.items(), key=lambda item: item[1])
        stop = min(max(ends.values()), utc_day(config.end))
        if stop - short > timedelta(days=1):  # a stream ends early: Binance delisted it?
            raise ValueError(
                f"{symbol} {timeframe} data ends {short:%Y-%m-%d %H:%M}, before the period's end "
                f"{config.end}, and every stream would stop there: set end: {short:%Y-%m-%d}, "
                f"leave the symbol out, or {store.download_hint([(symbol, timeframe)])} if it "
                "is still listed"
            )


def check_holdout(config: ValidationConfig, base: BacktestConfig, store: BarStore) -> None:
    """The holdout runs last: data it needs must be there before anything runs."""
    ends = data_ends(base, store)
    if ends and min(ends.values()) <= utc_day(config.holdout_start):
        (symbol, timeframe), last = min(ends.items(), key=lambda item: item[1])
        raise ValueError(
            f"holdout_start {config.holdout_start} is after the stored {symbol} {timeframe} "
            f"bars (to {last:%Y-%m-%d %H:%M}): set it earlier, or "
            f"{store.download_hint([(symbol, timeframe)])}"
        )


def utc_day(day: date) -> datetime:
    return datetime.combine(day, time(), tzinfo=UTC)


def output_file(value: str | None, folder: Path, name: str) -> Path:
    """Where to write: the file named, or `name` in a folder (one that exists, or a path
    ending in a slash), or `name` in folder when nothing is named."""
    if not value:
        return folder / name
    path = typed_path(value)
    if path.is_dir():
        return path / name
    if value.endswith(("/", "\\")):  # a folder to create; a backslash ends one on any system
        return typed_path(value.rstrip("/\\")) / name
    return path if path.suffix else path.with_suffix(Path(name).suffix)  # `mydash`: mydash.html


def html_file(value: str | None, folder: Path, name: str) -> Path:
    """An output_file for a page: never a file of another kind, such as a ledger."""
    path = output_file(value, folder, name)
    if path.suffix.lower() not in (".html", ".htm"):
        raise ConfigError(f"{path}: the page is HTML; name a .html file or a folder")
    return path


def run_backtest_command(args: argparse.Namespace) -> int:
    config = load(args.config, load_config, ("backtest",))
    check_strategies(args.config, config)
    out = None  # a page named wrongly fails now, not after the run
    if args.html is not None:
        out = html_file(args.html, Path("reports"), f"{args.config.stem}.html")
    check_period(config, BarStore(args.data_dir))
    print_notes(config, BarStore(args.data_dir))
    print("running the backtest...", file=sys.stderr, flush=True)
    result = run_backtest(config, BarStore(args.data_dir))
    metrics = compute_metrics(result)
    trial_log(args.data_dir).append([make_record(config, metrics, "backtest")])
    print(format_metrics(metrics))
    print(PAST_RESULTS)
    for symbol, quantity in sorted(result.positions.items()):
        print(f"Open position {symbol} {quantity:.6f}")
    if result.halted:
        print(f"Kill switch  {result.halted}; flat from then on")
    if args.attribution:
        print("running each strategy alone...", file=sys.stderr, flush=True)
        attribution = run_attribution(config, BarStore(args.data_dir))
        solo = [
            make_record(
                config.model_copy(
                    update={"strategies": [s.model_copy(update={"allocation": 1.0})]}
                ),
                row.metrics,
                "attribution",
            )
            for s, row in zip(config.strategies, attribution.rows, strict=False)
        ]
        trial_log(args.data_dir).append(solo)  # looks at the period, not selections
        print()
        print(format_attribution(attribution))
    if out is not None:
        subtitle = f"{args.config}, {config.start} to {config.end or 'latest'}"
        data = from_backtest(result, f"Backtest {args.config.stem}", subtitle)
        write_dashboard(data, out, show=not args.no_open)
    return 0


def write_dashboard(data: DashboardData, out: Path, *, show: bool = False) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(data), encoding="utf-8")
    print(f"wrote {out}")
    if show and sys.stdout.isatty():  # a person at a terminal, not a script
        webbrowser.open(out.resolve().as_uri())


def run_dashboard_command(args: argparse.Namespace) -> int:
    path = args.config or pick_config("dashboard")
    config = existing_ledger(load_session_config(path), path)
    out = html_file(args.out, Path("reports"), f"{path.stem}.html")
    kind = config.mode.capitalize() if isinstance(config, LiveConfig) else "Paper"
    title = f"{kind} session {path.stem}"
    write_dashboard(from_ledger(config, BarStore(args.data_dir), title), out, show=not args.no_open)
    return 0


def run_validate_command(args: argparse.Namespace) -> int:
    config, base = load(args.config, load_validation_config, ("validation",))
    check_period(base, BarStore(args.data_dir))
    check_holdout(config, base, BarStore(args.data_dir))
    print_notes(base, BarStore(args.data_dir))
    report = run_validation(
        config,
        base,
        args.data_dir,
        trial_log(args.data_dir),
        workers=args.workers,
        progress=lambda stage: print(f"... {stage}", flush=True),
    )
    print(format_report(report))
    return 0 if report.passed else 1


def run_paper_command(args: argparse.Namespace) -> int:
    config = load_session_config(args.config)
    if not isinstance(config, PaperConfig):
        raise ConfigError(f"this is a live config; use `tbot live {short_name(args.config)}`")
    check_strategies(args.config, config)
    settings = Settings()  # a broken .env fails here, before the session says it starts
    not_running(config)
    log_file = args.log_file or log_path(args.config)
    announce("paper", args.config, config, log_file)
    session_logging(log_file)
    return asyncio.run(run_paper(config, settings, args.data_dir, name=short_name(args.config)))


def run_live_command(args: argparse.Namespace) -> int:
    config = load_live_config(args.config)
    if config.mode == "live" and not args.live:
        raise ConfigError("config mode is live: pass --live to trade real money")
    if config.mode != "live" and args.live:
        raise ConfigError("--live is only for mode: live; this config trades on the testnet")
    check_strategies(args.config, config)
    settings = Settings()
    if settings.api_keys(config.mode) is None:
        raise ConfigError(KEYS_MISSING)  # every start would fail the same way
    not_running(config)
    asked = config.mode == "live" and interactive() and not supervised()
    if asked and not confirm_budget(args.config, config):
        print("that is not the budget in the config: nothing started", file=sys.stderr)
        return EXIT_ERROR
    log_file = args.log_file or log_path(args.config)
    announce(config.mode, args.config, config, log_file)
    session_logging(log_file)
    name = short_name(args.config)
    return asyncio.run(run_live(config, settings, args.data_dir, confirmed=args.live, name=name))


QUOTES = ("FDUSD", "USDT", "USDC", "TUSD", "EUR", "TRY", "BTC", "ETH", "BNB")


def quote_of(config: SessionConfig) -> str:
    """The currency initial_cash is in: the quote asset every symbol shares."""
    found = {
        next((q for q in QUOTES if symbol.endswith(q)), "")
        for c in config.strategies
        for symbol in c.symbols
    }
    return found.pop() if len(found) == 1 and "" not in found else "of the quote currency"


def stored_budget(config: SessionConfig) -> float | None:
    """The budget the ledger's book was last given, or None before the first start."""
    if not config.ledger.is_file():
        return None
    with Ledger(config.ledger) as ledger:
        raw = ledger.get_meta(BUDGET_META)
    return float(raw) if raw is not None else None


def confirm_budget(path: Path, config: LiveConfig) -> bool:
    """A real-money book that takes a budget for the first time, or a raised one, starts
    only once the owner types the amount: a mistyped initial_cash must not trade."""
    before = stored_budget(config)
    if before is not None and config.initial_cash <= before:
        return True
    name, quote = short_name(path), quote_of(config)
    raised = f", raised from {before:,.2f}" if before is not None else ""
    drawdown = config.guard.max_drawdown
    lines = [
        f"REAL MONEY: {path} lets the bot trade up to {config.initial_cash:,.2f} {quote} of "
        f"this Binance account (initial_cash{raised})."
        if config.ownership == "budget"
        else f"REAL MONEY: {path} lets the bot take over this whole Binance spot account "
        "(ownership: account): all of its cash and every coin of the pairs it trades.",
    ]
    if drawdown:
        lines.append(
            f"The kill switch sells everything after a {drawdown:.0%} fall from the best value "
            f"(about {config.initial_cash * drawdown:,.2f} of this budget)."
        )
    if config.protective_stop_pct:
        lines.append(
            f"Each position also has an exchange stop {config.protective_stop_pct:.0%} under "
            "the last close."
        )
    lines.append(f"`tbot halt {name}` sells everything and stops its trading at any time.")
    print("\n".join(lines))
    if config.ownership == "account":
        return input("To start, type account: ").strip().lower() == "account"
    typed = input(f"To start, type the budget ({config.initial_cash:,.2f}): ")
    return amount_matches(typed, config.initial_cash)


def amount_matches(typed: str, amount: float) -> bool:
    """`1,000`, `1000 usdt`, and `1000.0` all match 1000."""
    text = re.sub(r"[\s,_]", "", typed.lower())
    text = re.sub(r"[a-z]+$", "", text)
    try:
        return abs(float(text) - amount) <= 1e-9 * max(1.0, amount)
    except ValueError:
        return False


def session_logging(log_file: Path) -> None:
    """Everything to the log file; under a supervisor, whose console file is never rotated,
    only warnings and errors to the console."""
    configure_logging(log_file, console_level=logging.WARNING if supervised() else logging.INFO)


def not_running(config: SessionConfig) -> None:
    # Waits as the session's own lock does: `tbot status` may hold it for a moment.
    if config.ledger.is_file() and is_running(config.ledger, wait=3.0):
        raise AlreadyRunning(f"a session already runs on {config.ledger}: `tbot status` shows it")


def announce(kind: str, path: Path, config: SessionConfig, log_file: Path) -> None:
    """What runs and how to stop it, before the log takes over the console."""
    name = short_name(path)
    shared = sharing_note(path, config)
    timeframe, first = next_close(config, datetime.now(UTC))
    money = ""
    if isinstance(config, LiveConfig) and config.mode == "live":
        stake = (
            f"up to {config.initial_cash:,.2f} {quote_of(config)} (initial_cash)"
            if config.ownership == "budget"
            else "the whole spot account (ownership: account)"
        )
        money = f"REAL MONEY: {stake}; `tbot halt {name}` sells everything and halts\n"
    print(
        f"{kind} session {name} (tbot {__version__}): ledger {config.ledger}, log {log_file}\n"
        f"{money}"
        f"its first decision comes with the next {timeframe} close, {clock_text(first)}; "
        f"`tbot status {name}` shows what it did\n"
        f"stop it with Ctrl+C here, or `tbot stop {name}` from another terminal"
        + (f"\nnote: {shared}" if shared else ""),
        file=sys.stderr,
        flush=True,
    )


def log_path(config: Path) -> Path:
    """One log per config, so two sessions never rotate the same file: a config in
    config/ by its name, any other also by a hash of where it is."""
    full = config.resolve()
    if full.parent == Path("config").resolve():  # the repository: run() starts there
        return Path("logs") / f"{config.stem}.jsonl"
    digest = hashlib.sha256(str(full).lower().encode()).hexdigest()[:8]  # Windows ignores case
    return Path("logs") / f"{config.stem}-{digest}.jsonl"


def run_status_command(args: argparse.Namespace) -> int:
    store = BarStore(args.data_dir)
    if args.config is None:
        sessions, broken = scan_configs()
        rows = []
        for path, config in sessions:
            row = session_row(path.stem, config, store)
            shared = sharing_note(path, config)
            rows.append(replace(row, notes=(*row.notes, shared)) if shared else row)
        rows += [broken_row(b.path.stem, b.problem, b.ledger) for b in broken]
        print(f"tbot {__version__}")
        print(overview_text(rows))
        return 0
    config = existing_ledger(load_session_config(args.config), args.config)
    print(status_text(config, store))
    return 0


def run_log_command(args: argparse.Namespace) -> int:
    if args.file is None and args.config is not None:
        kind = kind_of(read_yaml(args.config))  # a typo is no config, not a log yet to come
        if kind not in ("paper", "live"):
            raise ConfigError(f"{args.config} is a {kind} config: it prints, and keeps no log")
    path = args.file or log_path(args.config or pick_config("log"))
    minimum = LEVELS.index(args.level)
    if isinstance(sys.stdout, io.TextIOWrapper):  # a console that cannot show a character
        sys.stdout.reconfigure(errors="replace")
    size = 0
    if path.is_file():
        lines, size = tail(path, args.lines, minimum)
        if lines:
            print("\n".join(lines))
        elif args.lines > 0:
            print(f"{path} has no lines at level {args.level} or above")
    elif not args.follow:
        print(f"no log at {path} yet: a session writes it while it runs")
        return EXIT_ERROR
    if args.follow:
        print(f"following {path}; Ctrl+C ends", flush=True)
        with suppress(KeyboardInterrupt):
            follow(path, lambda line: print(line, flush=True), start=size, minimum=minimum)
    return 0


def run_doctor_command(args: argparse.Namespace) -> int:
    path = args.config or pick_config("doctor")
    config = load_session_config(path)

    async def check() -> list[Check]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            store = BarStore(args.data_dir)
            return await run_checks(config, Settings(), client, store, offline=args.offline)

    checks = asyncio.run(check())
    shared = sharing_note(path, config)
    if shared:
        checks.insert(1, Check("warn", "ledger", shared))
    print(checks_text(checks))
    return 1 if any(c.status == "fail" for c in checks) else 0


def run_stop_command(args: argparse.Namespace) -> int:
    if args.cancel:
        if args.config:
            paths = [args.config]
        else:
            sessions, broken = scan_configs()
            paths = [path for path, _ in sessions] + [b.path for b in broken if b.ledger]
        withdrawn = False
        for path in paths:
            request = stop_path(stop_ledger(path))
            if request.exists():
                request.unlink(missing_ok=True)
                print(f"stop request for {short_name(path)} withdrawn")
                withdrawn = True
        if not withdrawn:
            print("no stop request")
        return 0
    path = args.config
    if path is None:
        running = running_configs()
        if len(running) > 1:
            raise ConfigError(which("stop", running, "running"))
        if running:
            path = picked(running[0])
        else:
            started = [p for p, config in session_configs() if config.ledger.is_file()]
            if len(started) != 1:
                print(f"no session from {CONFIG_DIR}/ is running (another file: tbot stop <file>)")
                return 0
            path = picked(started[0])  # its supervisor may be about to start it again
    ledger = stop_ledger(path)
    if not ledger.is_file():
        raise ConfigError(f"no ledger at {ledger}: nothing has run with this config yet")
    request = stop_path(ledger)
    request.write_text("stop\n", encoding="utf-8")  # also stops a restart by the supervisor
    later = (
        f"a supervisor's restart in the next {STOP_REQUEST_SECONDS // 60} minutes stops at "
        f"once (`tbot stop --cancel` withdraws it)"
    )
    if not is_running(ledger):
        print(f"no session is running on {ledger}; {later}")
        note = after_stop(path, ledger)
        if note:
            print(note)
        return 0
    print(f"asked the session on {ledger} to stop; it finishes the event in progress")
    deadline = monotonic() + args.timeout
    while monotonic() < deadline:
        sleep(1.0)
        if not is_running(ledger):
            if request.exists():  # it ended some other way, such as a crash
                print(f"the session ended without taking the request; {later}")
            else:
                print("stopped")
            note = after_stop(path, ledger)
            if note:
                print(note)
            return 0
    print(f"still running: `tbot log {short_name(path)}` shows what it is doing", file=sys.stderr)
    return EXIT_ERROR


def after_stop(path: Path, ledger: Path) -> str:
    """What still happens to a stopped session: a supervisor brings it back at the next boot,
    and its positions stay (with their exchange stops)."""
    try:
        config = load_session_config(path)
    except ConfigError:  # it no longer loads: `tbot doctor` says why
        return ""
    name = short_name(path)
    with Ledger(ledger) as book:
        scheduled = book.get_meta(SUPERVISED_META) == "1"
        positions = restore_portfolio(config, book).positions
    notes = []
    if scheduled:
        keep = (
            f"`tbot autostart {name} -Remove`"
            if sys.platform == "win32"
            else "disabling its systemd unit"
        )
        notes.append(f"its scheduled task starts it again at the next boot; {keep} prevents that")
    if any(positions.values()):
        guarded = isinstance(config, LiveConfig) and config.protective_stop_pct > 0
        stops = ", with their exchange stops," if guarded else ""
        notes.append(
            f"its positions stay{stops} until it runs again; `tbot halt {name}` sells them"
        )
    return "\n".join(notes)


def run_compare_command(args: argparse.Namespace) -> int:
    path = args.config or pick_config("compare")
    config = existing_ledger(load_session_config(path), path)
    with Ledger(config.ledger) as ledger:
        try:
            comparison = compare_session(config, ledger, BarStore(args.data_dir))
        except ValueError as exc:
            print(f"cannot compare: {exc}", file=sys.stderr)
            return EXIT_ERROR
    print(comparison_text(comparison, str(config.ledger)))
    return 0 if not comparison.checks() else 1


def run_backup_command(args: argparse.Namespace) -> int:
    path = args.config or pick_config("backup")
    config = existing_ledger(load_session_config(path), path)
    name = f"{config.ledger.stem}-{datetime.now(UTC):%Y%m%d-%H%M%S}.sqlite"
    out = output_file(args.out, backup_dir(config.ledger), name)  # a folder: a synced one
    if exists(out):  # another backup, or a ledger: never written over
        raise ConfigError(f"{out} exists: name a new file, or a folder")
    with Ledger(config.ledger) as ledger:
        ledger.backup(out)
    print(f"wrote {out}")
    return 0


SECRET_SHAPES = (
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),  # a Telegram bot token
    re.compile(r"\b[A-Za-z0-9]{64}\b"),  # a Binance key or secret
    re.compile(r"https?://[^\s\"']*(?:ping|heartbeat|push)[^\s\"']*", re.IGNORECASE),
)
SUPPORT_LOG_LINES = 40


def redact(text: str, secrets: list[str]) -> str:
    """The text without the secrets given, nor anything shaped like a key or a ping URL."""
    for secret in sorted((s for s in secrets if len(s) >= 6), key=len, reverse=True):
        text = text.replace(secret, "[secret]")
    for shape in SECRET_SHAPES:
        text = shape.sub("[secret]", text)
    return text


def support_text(store: BarStore) -> str:
    """Versions, configs, offline doctor checks, the overview, and recent problems in logs."""
    settings = Settings()
    found = [name.upper() for name, value in settings.model_dump().items() if value]
    lines = [
        f"tbot {__version__} support report, {datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
        f"python {platform.python_version()} on {platform.platform()}",
        f"settings in .env: {', '.join(f'TBOT_{name}' for name in found) or 'none'}",
        "",
    ]
    sessions, broken = scan_configs()
    rows = [session_row(path.stem, config, store) for path, config in sessions]
    rows += [broken_row(b.path.stem, b.problem, b.ledger) for b in broken]
    lines += ["== sessions", overview_text(rows), ""]

    async def offline(config: SessionConfig) -> list[Check]:
        async with httpx.AsyncClient() as client:
            return await run_checks(config, settings, client, store, offline=True)

    for path, config in sessions:
        lines += [f"== doctor --offline {path}", checks_text(asyncio.run(offline(config))), ""]
    warning = LEVELS.index("warning")
    for log in sorted(Path("logs").glob("*.jsonl")):
        recent, _ = tail(log, SUPPORT_LOG_LINES, warning)
        lines += [f"== {log}: the last {SUPPORT_LOG_LINES} warnings and errors", *recent, ""]
    for journal in sorted(Path("logs").glob("*.supervisor.log")):
        text = journal.read_text(encoding="utf-8", errors="replace").splitlines()
        lines += [f"== {journal}", *text[-SUPPORT_LOG_LINES:], ""]
    secrets = [str(value) for value in settings.model_dump().values() if value]
    return redact("\n".join(lines), secrets)


def run_support_command(args: argparse.Namespace) -> int:
    name = f"support-{datetime.now(UTC):%Y%m%d-%H%M%S}.txt"
    out = output_file(args.out, Path("reports"), name)
    if exists(out):
        raise ConfigError(f"{out} exists: name a new file, or a folder")
    text = support_text(BarStore(args.data_dir))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n", encoding="utf-8")
    print(
        f"wrote {out}: versions, configs, checks, and recent warnings, without keys or tokens. "
        "Read it before you share it: it shows balances and trades."
    )
    return 0


def run_resume_command(args: argparse.Namespace) -> int:
    path = args.config
    if path is None:
        halted = [
            path
            for path, config in session_configs()
            if config.ledger.is_file() and is_halted(config)
        ]
        if len(halted) > 1:
            raise ConfigError(which("resume", halted, "halted"))
        if not halted:
            print("no session is halted")
            return 0
        config = load_session_config(halted[0])
        if isinstance(config, LiveConfig) and config.mode == "live":
            raise ConfigError(
                f"the halted session trades real money: name it to resume it: "
                f"tbot resume {short_name(halted[0])}"
            )
        path = picked(halted[0])
    config = existing_ledger(load_session_config(path), path)
    real_money = isinstance(config, LiveConfig) and config.mode == "live"
    if is_halted(config):
        print(resume_note(config))
        if real_money and interactive() and input("Type resume to go on: ").strip() != "resume":
            print("nothing changed: still halted")
            return EXIT_ERROR
    print(resume(config))
    return 0


def resume_note(config: SessionConfig) -> str:
    """What a resume means for the book: where it stands, and what trips it again."""
    with Ledger(config.ledger) as ledger:
        state = load_guard(config, ledger).state
        point = ledger.latest_equity()
    lines = [f"halted: {state.halt_reason}"]
    if point is not None and state.peak_equity > 0:
        lines.append(
            f"equity {point.equity:,.2f} at the last bar event, "
            f"{point.equity / state.peak_equity - 1:.1%} from its best of {state.peak_equity:,.2f}"
        )
    limit = config.guard.max_drawdown
    again = f"another {limit:.0%} fall from here halts it again; " if limit else ""
    lines.append(
        f"after resume the kill switch counts from the equity then: {again}the strategy may "
        "buy back in at the next bar"
    )
    return "\n".join(lines)


def run_halt_command(args: argparse.Namespace) -> int:
    """The panic button: the session sells what the bot holds and trades no more."""
    path = args.config
    if path is None:
        path = pick_config("halt")
        picked_config = load_session_config(path)
        if isinstance(picked_config, LiveConfig) and picked_config.mode == "live":
            raise ConfigError(
                f"it trades real money: name it to sell and halt it: tbot halt {short_name(path)}"
            )
    name = short_name(path)
    config: SessionConfig | None = None
    try:
        config = existing_ledger(load_session_config(path), path)
        ledger_path = config.ledger
    except ConfigError:  # edited while it runs: the request needs only the ledger
        ledger_path = stop_ledger(path)
        if not (ledger_path.is_file() and is_running(ledger_path)):
            raise
    if config is not None:
        try:
            with instance_lock(ledger_path, wait=3.0):  # none runs, and none starts meanwhile
                print(halted_text(name, config, *halt_book(config)))
                return 0
        except AlreadyRunning:
            pass
    request = halt_path(ledger_path)
    request.write_text("halt\n", encoding="utf-8")
    print(f"asked the session on {ledger_path} to sell what the bot holds and halt", flush=True)
    deadline = monotonic() + args.timeout
    while monotonic() < deadline:
        sleep(1.0)
        if not request.exists():  # taken and done
            if config is None:
                print(f"halted; `tbot status {name}` shows what it holds")
                return 0
            with Ledger(ledger_path) as ledger:
                halted = load_guard(config, ledger).state.halted
                positions = restore_portfolio(config, ledger).positions
            if not halted:
                print(f"the session did not halt: `tbot log {name}` says why", file=sys.stderr)
                return EXIT_ERROR
            print(f"halted: {held_names(positions)}; `tbot resume {name}` trades again")
            return 0
        if config is not None and not is_running(ledger_path):  # it ended first
            try:
                with instance_lock(ledger_path, wait=3.0):
                    request.unlink(missing_ok=True)
                    print(halted_text(name, config, *halt_book(config)))
                return 0
            except AlreadyRunning:  # a restart took it, and takes the request
                continue
    print(
        f"not done yet: the request stays until the session has sold; `tbot log {name} -f` "
        "shows what it does",
        file=sys.stderr,
    )
    return EXIT_ERROR


def held_list(positions: dict[str, float]) -> list[str]:
    return [f"{symbol} {qty:.6f}" for symbol, qty in sorted(positions.items()) if qty]


def held_names(positions: dict[str, float]) -> str:
    held = held_list(positions)
    return f"still held: {', '.join(held)} (see the alerts)" if held else "nothing held"


def halted_text(
    name: str, config: SessionConfig, already: bool, reason: str, positions: dict[str, float]
) -> str:
    """What `tbot halt` did to a session that was not running."""
    held = held_list(positions)
    head = (
        f"{name} was halted already ({reason})"
        if already
        else f"halted {name} (no session was running)"
    )
    if not held:
        return f"{head}: the bot holds nothing, and it trades no more until `tbot resume {name}`"
    text = (
        f"{head}: its next start sells {', '.join(held)} at once and trades no more until "
        f"`tbot resume {name}`"
    )
    if isinstance(config, LiveConfig):  # a sale by hand would not leave the book
        stops = "the exchange stops stay in place; " if config.protective_stop_pct else ""
        text += f". Until then {stops}to sell now, start it (`{live_command(name, config)}`)"
    return text


def is_halted(config: SessionConfig) -> bool:
    try:
        with Ledger(config.ledger) as ledger:
            return load_guard(config, ledger).state.halted
    except Exception:  # an unusable ledger is reported when it is named
        return False


def existing_ledger[C: SessionConfig](config: C, path: Path) -> C:
    """Opening a ledger creates it; commands that only read must not leave one behind."""
    if not config.ledger.is_file():
        name = short_name(path)
        start = (
            f"real money: read README, Before real money, then `tbot doctor {name}`"
            if isinstance(config, LiveConfig) and config.mode == "live"
            else f"`{live_command(name, config)}` starts it"
        )
        raise NothingYet(
            f"no ledger at {config.ledger}: nothing has run with this config yet "
            f"({start}; `tbot status` lists every session)"
        )
    return config


def run_account_command(args: argparse.Namespace) -> int:
    config = load_live_config(args.config or pick_config("account", live=True))
    settings = Settings()
    if settings.api_keys(config.mode) is None:
        raise ConfigError(KEYS_MISSING)
    print(asyncio.run(account_text(config, settings)))
    return 0


def interactive() -> bool:
    """A person at a terminal who can answer questions."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def run_notify_command(args: argparse.Namespace) -> int:
    """Set up Telegram step by step, saving what it learns in .env, then send a test."""
    settings = Settings()
    token, chat_id = settings.telegram_token, settings.telegram_chat_id
    new_token = not token
    if not token:
        if not interactive():
            print("set TBOT_TELEGRAM_TOKEN (from @BotFather) in .env, or run `tbot notify` in a")
            print("terminal to be guided")
            return EXIT_ERROR
        print("Telegram alerts: in Telegram, open @BotFather, send /newbot, and follow it.")
        token = input("Paste the token it gives you here: ").strip()
        if not token:
            print("no token: nothing changed")
            return EXIT_ERROR
    if args.chat:
        chat_id = None
    code = asyncio.run(set_up_telegram(token, chat_id, new_token, Path(".env")))
    if code == 0 and (new_token or not settings.telegram_chat_id or args.chat):
        print_restart_note()
    return code


def print_restart_note() -> None:
    """Sessions read .env when they start."""
    running = [short_name(path) for path in running_configs()]
    if running:
        print(
            f"running sessions use the new settings once restarted: {', '.join(running)} "
            "(`tbot stop <config>`, then start it again)"
        )


async def set_up_telegram(token: str, chat_id: str | None, new_token: bool, env: Path) -> int:
    async with httpx.AsyncClient() as client:
        bot = Telegram(token, chat_id or "", client)
        try:
            name = await bot.me()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 404):
                print("Telegram does not know this token: copy it again from @BotFather")
                if new_token or not interactive():
                    print(f"(it is TBOT_TELEGRAM_TOKEN in {env})")
                    return EXIT_ERROR
                token = input("Paste the new token here, or press Enter to stop: ").strip()
                if not token:
                    return EXIT_ERROR
                return await set_up_telegram(token, chat_id, True, env)
            print(f"cannot reach Telegram ({describe_error(exc)})")
            return EXIT_ERROR
        except httpx.HTTPError as exc:
            print(f"cannot reach Telegram ({describe_error(exc)})")
            return EXIT_ERROR
        if new_token:
            save_settings(env, {"TBOT_TELEGRAM_TOKEN": token})
            print(f"saved the token of @{name} in {env}")
        if not chat_id:
            chat_id = await find_chat(bot, name, token)
            if chat_id is None:
                return EXIT_ERROR
            save_settings(env, {"TBOT_TELEGRAM_CHAT_ID": chat_id})
            print(f"saved chat {chat_id} in {env}")
            bot.chat_id = chat_id
        if await bot.send("tbot: test alert. Alerts from the bot arrive in this chat."):
            print(f"sent a test message from @{name}: check the Telegram chat")
            return 0
        print("failed: see the log line above; check TBOT_TELEGRAM_CHAT_ID in .env")
        return EXIT_ERROR


def one_time_code() -> str:
    """Letters first: in a group it goes as a command, the only text bots there receive.
    Eight hex digits: four billion codes, none a stranger can guess in two minutes."""
    return f"tb{secrets.token_hex(4)}"


async def find_chat(bot: Telegram, name: str, token: str = "") -> str | None:
    """The chat that sends the bot a one-time code. Bot names are public: a stranger who
    wrote to the bot first must not end up with the alerts."""
    if not interactive():
        print("run `tbot notify` in a terminal to pick the chat, or set TBOT_TELEGRAM_CHAT_ID")
        return None
    turn = PollTurn(token, RANK_PAIRING)  # a running session lets go of the messages
    try:
        if not await take_turn(turn):
            return None
        return await pair_chat(bot, name)
    finally:
        turn.release()


async def take_turn(turn: PollTurn) -> bool:
    if turn.take():
        return True
    print("a running session reads the bot's messages: asking it to let go (up to 90 seconds)")
    deadline = monotonic() + TURN_WAIT_SECONDS
    while monotonic() < deadline:
        await asyncio.sleep(1.0)
        if turn.take():
            return True
    print("it did not let go: stop the session (`tbot stop`), then run `tbot notify` again")
    return False


async def pair_chat(bot: Telegram, name: str) -> str | None:
    code = one_time_code()
    print(f"Now send this code to @{name} in Telegram: {code} (waiting up to 2 minutes)")
    print(f"For alerts in a group, add the bot to it and send /{code}@{name} there.")
    deadline = monotonic() + CHAT_WAIT_SECONDS
    shown = datetime.now(UTC).timestamp() - 60  # a minute of slack for this clock
    try:
        # Only what is sent from now on counts: a stranger cannot have sent this code
        # before, but could have sent every code of a shorter kind.
        backlog = await bot.updates(-1, 0)
        offset = int(backlog[-1]["update_id"]) + 1 if backlog else None
        while monotonic() < deadline:
            for update in await bot.updates(offset, 10):
                offset = int(update["update_id"]) + 1
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                text = command_of(str(message.get("text") or "")).lstrip("/")
                fresh = float(message.get("date") or 0) >= shown
                if "id" in chat and fresh and text == code:
                    title = chat.get("title") or chat.get("first_name") or chat.get("username")
                    print(f"found chat {chat['id']} {title or ''}".rstrip())
                    with suppress(httpx.HTTPError):  # done with: no session answers it again
                        await bot.updates(offset, 0)
                    return str(chat["id"])
    except httpx.HTTPError as exc:
        busy = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 409
        print(
            f"cannot read the bot's messages ({describe_error(exc)})"
            + (
                ": another program reads them, such as a running session with "
                "telegram_commands, or a webhook"
                if busy
                else ""
            )
        )
        return None
    print(f"the code did not reach @{name}: run `tbot notify` again and send the new code")
    return None


KEY_SOURCES = {
    "testnet": (
        "sign in at https://testnet.binance.vision with GitHub, choose Generate "
        "HMAC_SHA256 Key, and copy the API key and the secret key"
    ),
    "live": (
        "on binance.com open API Management and create a system-generated (HMAC) key; turn "
        "on spot trading, leave withdrawals off (tbot refuses a key that can withdraw), and "
        "restrict it to your IP if yours never changes"
    ),
}
HMAC_SHAPE = re.compile(r"[A-Za-z0-9]{64}")
KEY_CHECKS = {"binance", "api key", "permissions"}  # what a key that fails must not pass


def run_keys_command(args: argparse.Namespace) -> int:
    """Ask for a key pair, check it with Binance, and save it in .env only once it works."""
    config = load_live_config(args.config)
    name = short_name(args.config)
    if not interactive():
        print("run `tbot keys` in a terminal: it asks for the key and the secret, checks them")
        print("with Binance, and saves them in .env")
        return EXIT_ERROR
    print(f"Binance API keys for {name} ({config.mode}): {KEY_SOURCES[config.mode]}.")
    key = input("API key: ").strip()
    secret = getpass.getpass("Secret key (hidden while you type or paste): ").strip()
    if not key or not secret:
        print("nothing changed")
        return EXIT_ERROR
    if not (HMAC_SHAPE.fullmatch(key) and HMAC_SHAPE.fullmatch(secret)):
        print(
            "note: system-generated (HMAC) keys are 64 letters and digits; tbot cannot sign "
            "with an Ed25519 or RSA key"
        )
    current = Settings()
    trial = current.model_copy(
        update={
            "binance_api_key": key,
            "binance_api_secret": secret,
            "binance_testnet_api_key": key,
            "binance_testnet_api_secret": secret,
        }
    )
    checks, old_is_testnet = asyncio.run(check_new_keys(config, trial, current))
    print(checks_text(checks))
    if any(c.status == "fail" and c.name in KEY_CHECKS for c in checks):
        print(f"not saved: fix what failed above, then run `tbot keys {name}` again")
        return EXIT_ERROR
    if config.mode == "testnet":
        names = ("TBOT_BINANCE_TESTNET_API_KEY", "TBOT_BINANCE_TESTNET_API_SECRET")
    else:
        names = ("TBOT_BINANCE_API_KEY", "TBOT_BINANCE_API_SECRET")
    values = dict(zip(names, (key, secret), strict=True))
    if old_is_testnet and current.binance_api_key and current.binance_api_secret:
        values["TBOT_BINANCE_TESTNET_API_KEY"] = current.binance_api_key
        values["TBOT_BINANCE_TESTNET_API_SECRET"] = current.binance_api_secret
    env = Path(".env")
    save_settings(env, values)
    print(f"saved in {env} as {names[0]} and {names[1]}, readable by your account only")
    if old_is_testnet:
        print("the testnet keys there before now are TBOT_BINANCE_TESTNET_API_KEY and _SECRET")
    print_restart_note()
    return 0


async def check_new_keys(
    config: LiveConfig, trial: Settings, current: Settings
) -> tuple[list[Check], bool]:
    """Doctor's checks of the new key, and whether the live key it replaces in the main
    pair is a testnet key, which then moves to the testnet pair so testnet keeps working."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        checks = await key_checks(config, trial, client)
        old = (current.binance_api_key, current.binance_api_secret)
        moves = (
            config.mode == "live"
            and current.api_keys("testnet") == old
            and all(old)
            and old[0] != trial.binance_api_key
        )
        return checks, moves and await works_on_testnet(old[0] or "", old[1] or "", client)


COMMANDS = {
    "download": run_download,
    "check": run_check,
    "backtest": run_backtest_command,
    "validate": run_validate_command,
    "paper": run_paper_command,
    "live": run_live_command,
    "status": run_status_command,
    "log": run_log_command,
    "compare": run_compare_command,
    "doctor": run_doctor_command,
    "stop": run_stop_command,
    "backup": run_backup_command,
    "support": run_support_command,
    "halt": run_halt_command,
    "resume": run_resume_command,
    "account": run_account_command,
    "dashboard": run_dashboard_command,
    "notify": run_notify_command,
    "keys": run_keys_command,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["help"]:  # `tbot help`, `tbot help paper`
        if argv[1:2] and argv[1] in SCRIPTS:
            print(f"`tbot {argv[1]}` runs a Windows script: `tbot {argv[1]} -h` shows its help")
            return 0
        argv = [*argv[1:2], "-h"]
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse: 0 after --help, 2 for a wrong command line
        return EXIT_CONFIG if exc.code == 2 else int(exc.code or 0)
    if args.command not in COMMANDS:
        parser.print_help()
        return 0
    session = args.command in ("paper", "live")
    if not session:
        configure_quiet()  # warnings only, readable; a session sets up its own log
    try:
        return COMMANDS[args.command](args)
    except ConfigError as exc:
        return _fail(str(exc.code), EXIT_CONFIG, alert=session)
    except NothingYet as exc:
        return _fail(str(exc), EXIT_ERROR, alert=False)
    except (LedgerUnavailable, AlreadyRunning) as exc:
        return _fail(str(exc), EXIT_LEDGER, alert=session and isinstance(exc, LedgerUnavailable))
    except (LedgerModeError, StartRefused) as exc:
        return _fail(f"error: {exc}", EXIT_CONFIG, alert=session)
    except UnicodeDecodeError as exc:  # .env or a config saved as UTF-16: fix the file
        return _fail(f"error: {brief(exc)}", EXIT_CONFIG, alert=False)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # one line; TBOT_DEBUG=1 shows the traceback
        if os.environ.get("TBOT_DEBUG", "").lower() in ("1", "true", "yes"):
            raise
        # A session that dies before or after it could alert (a ledger it cannot write)
        # says so here: a supervisor would restart it without a word.
        return _fail(f"error: {brief(exc)}", EXIT_ERROR, alert=session, what="stopped")


def run() -> int:
    """`tbot` and `python -m tbot`: run in the repository, wherever the command is typed.

    .env, config/, data/, and logs/ are relative paths; from another folder a session would
    start a new book there. tbot.cmd does the same on Windows.
    """
    global ORIGIN
    here = Path.cwd()
    if (ROOT / "pyproject.toml").is_file() and (ROOT / "config").is_dir() and here != ROOT:
        os.chdir(ROOT)
        ORIGIN = here  # files the user names stay relative to where they typed them
    return main()


def _fail(message: str, code: int, *, alert: bool, what: str = "cannot start") -> int:
    print(message, file=sys.stderr)
    if alert:  # a supervised bot that cannot start says so before the supervisor gives up
        _alert(f"tbot {what}: {message}")
    return code


def _alert(text: str) -> None:
    try:
        settings = Settings()
        token, chat_id = settings.telegram_token, settings.telegram_chat_id
        if not token or not chat_id:
            return

        async def send() -> None:
            async with httpx.AsyncClient() as client:
                await Telegram(token, chat_id, client).send(text)

        asyncio.run(send())
    except Exception:  # best effort: the message is already on stderr
        pass
