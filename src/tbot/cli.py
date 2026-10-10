"""Command line entry point."""

import argparse
import asyncio
import io
import os
import sys
import webbrowser
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import httpx
import yaml
from pydantic import ValidationError

from tbot import __version__
from tbot.backtest.attribution import format_attribution, run_attribution
from tbot.backtest.config import BacktestConfig, load_config
from tbot.backtest.metrics import compute_metrics
from tbot.backtest.report import format_metrics
from tbot.backtest.runner import data_ends, history_bars, run_backtest
from tbot.core.config import TradingConfig, load_yaml
from tbot.core.timeframe import Timeframe
from tbot.data.downloader import sync
from tbot.data.http import describe_error, retry_after
from tbot.data.quality import QualityReport, check_bars
from tbot.data.store import BarStore
from tbot.live.backup import backup_dir
from tbot.live.clock import CLOSE_GRACE, ServerClock
from tbot.live.compare import compare_session, comparison_text
from tbot.live.config import LiveConfig, PaperConfig, SessionConfig, Settings, save_settings
from tbot.live.doctor import Check, checks_text, run_checks
from tbot.live.ledger import Ledger, LedgerUnavailable
from tbot.live.overview import broken_row, overview_text, session_row
from tbot.live.runner import (
    KEYS_MISSING,
    STOP_REQUEST_SECONDS,
    AlreadyRunning,
    LedgerModeError,
    StartRefused,
    account_text,
    is_running,
    load_guard,
    resume,
    run_live,
    run_paper,
    status_text,
    stop_path,
)
from tbot.monitoring.dashboard import DashboardData, from_backtest, from_ledger, render
from tbot.monitoring.logging import configure_logging
from tbot.monitoring.logview import LEVELS, follow, tail
from tbot.monitoring.telegram import Telegram
from tbot.portfolio.allocation import build_slots
from tbot.research.report import format_report
from tbot.research.trials import TrialLog, make_record
from tbot.research.validate import load_validation_config, run_validation

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT"]
# Exit codes: scripts/run_bot.ps1 starts the bot again after 1 and gives up on 3 and 4.
EXIT_ERROR = 1  # a crash, a failed start, or an error worth retrying
EXIT_LEDGER = 3  # the ledger cannot be used: another process has it, it is damaged, no SQLite
EXIT_CONFIG = 4  # the command or the config is wrong: retrying cannot help


class ConfigError(SystemExit):
    """A wrong command or config; main() turns it into one line and EXIT_CONFIG."""


def brief(exc: BaseException) -> str:
    """One line for an error a user can act on."""
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'config'}: {error['msg']}"
            for error in exc.errors()
        )
    if isinstance(exc, UnicodeDecodeError):
        return f"a file is not UTF-8 text; save .env and configs as UTF-8 ({exc})"
    if isinstance(exc, httpx.HTTPStatusError):
        answer = exc.response.text[:200]
        return f"HTTP {exc.response.status_code} from {exc.request.url.host}: {answer}"
    if isinstance(exc, httpx.HTTPError):
        return f"network error: {exc!r}"
    return str(exc) or repr(exc)


DEFAULT_TIMEFRAMES = [Timeframe.H1, Timeframe.H4]
DEFAULT_START = "2017-08-01"
MAX_LISTED = 10
CONFIG_DIR = Path("config")
CONFIG_SUFFIXES = (".yaml", ".yml")
CHAT_WAIT_SECONDS = 120  # `tbot notify` waits this long for a first message to the bot


def config_arg(value: str) -> Path:
    """A config file, or the name of one in config/: `paper` is config/paper.yaml."""
    path = Path(value)
    if not path.exists() and not path.suffix and path.name == value:
        for suffix in CONFIG_SUFFIXES:
            named = CONFIG_DIR / f"{value}{suffix}"
            if named.is_file():
                return named
    return path


def config_files() -> list[Path]:
    return sorted(path for suffix in CONFIG_SUFFIXES for path in CONFIG_DIR.glob(f"*{suffix}"))


def config_names() -> list[str]:
    return sorted(path.stem for path in config_files())


def short_name(path: Path) -> str:
    """What finds this config again on the command line: `paper` for config/paper.yaml."""
    return path.stem if config_arg(path.stem).resolve() == path.resolve() else str(path)


def parse_symbol(value: str) -> str:
    return value.replace("/", "").upper()


def parse_date(value: str) -> datetime:
    return datetime.combine(date.fromisoformat(value), time(), tzinfo=UTC)


def fmt(moment: datetime | None) -> str:
    return f"{moment:%Y-%m-%d %H:%M}" if moment else "-"


def config_kind(raw: dict[str, Any]) -> str:
    """What a config file looks like, to point at the command that takes it."""
    if "backtest" in raw and "grid" in raw:
        return "a validation config (tbot validate)"
    if "mode" in raw:
        return "a testnet or live config (tbot live, doctor, stop, account, status, compare, ...)"
    if "ledger" in raw:
        return "a paper config (tbot paper, doctor, stop, status, compare, resume, dashboard)"
    if "start" in raw:
        return "a backtest config (tbot backtest)"
    return "not a tbot config"


def read_yaml(path: Path) -> dict[str, Any]:
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


def load[T](path: Path, loader: Callable[[Path], T]) -> T:
    """Run a config loader; a bad file ends the command with a readable message."""
    raw = read_yaml(path)
    try:
        return loader(path)
    except (ValidationError, ValueError) as exc:
        raise ConfigError(
            f"{path}: invalid for this command ({config_kind(raw)}): {brief(exc)}"
        ) from None
    except OSError as exc:  # a file the config points at, such as a validation's backtest
        raise ConfigError(f"{path}: {exc}") from None


def check_strategies(path: Path, config: TradingConfig) -> None:
    """Strategy names and params are checked when slots are built; do it before starting."""
    try:
        build_slots(config.strategies)
    except (ValidationError, ValueError) as exc:
        raise ConfigError(f"{path}: {brief(exc)}") from None


def load_session_config(path: Path) -> SessionConfig:
    """A config with `mode` is a live config; anything else is paper."""

    def parse(path: Path) -> SessionConfig:
        raw = read_yaml(path)
        if "mode" in raw:
            return LiveConfig.model_validate(raw)
        return PaperConfig.model_validate(raw)

    return load(path, parse)


def load_live_config(path: Path) -> LiveConfig:
    return load(path, lambda path: LiveConfig.model_validate(read_yaml(path)))


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
        if "ledger" not in raw and "mode" not in raw:
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
  tbot paper          paper trade with config/paper.yaml until stopped
  tbot status         every session at a glance
  tbot log -f         follow what a session does
  tbot stop           stop the running session after the event in progress

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
    status.add_argument("--data-dir", type=Path, default=Path("data"))

    paper = command("paper", "paper trade on live Binance data until stopped")
    paper.add_argument(
        "config", nargs="?", type=config_arg, default="paper", help="default: config/paper.yaml"
    )
    paper.add_argument("--data-dir", type=Path, default=Path("data"))
    paper.add_argument("--log-file", type=Path, help="default: logs/<config name>.jsonl")

    live = command("live", "trade on Binance testnet or live until stopped")
    live.add_argument("config", type=config_arg, help="testnet, live, or a config file")
    live.add_argument("--live", action="store_true", help="required when the config mode is live")
    live.add_argument("--data-dir", type=Path, default=Path("data"))
    live.add_argument("--log-file", type=Path, help="default: logs/<config name>.jsonl")

    stop_cmd = session(
        "stop", "ask a running session to stop after the event in progress", "the running one"
    )
    stop_cmd.add_argument("--timeout", type=float, default=90.0, help="seconds to wait")
    stop_cmd.add_argument(
        "--cancel", action="store_true", help="withdraw a request no session has taken yet"
    )

    log_cmd = session("log", "show a session's log, readable; -f keeps following it")
    log_cmd.add_argument("-n", "--lines", type=int, default=30, help="default: %(default)s")
    log_cmd.add_argument("-f", "--follow", action="store_true", help="print new lines until Ctrl+C")
    log_cmd.add_argument(
        "--level", choices=LEVELS, default="info", help="lowest level shown (default: info)"
    )
    log_cmd.add_argument("--file", type=Path, help="a log file to read instead, such as a .1")

    doctor = session("doctor", "check keys, alerts, network, and the ledger before a session")
    doctor.add_argument("--data-dir", type=Path, default=Path("data"))
    doctor.add_argument(
        "--offline",
        action="store_true",
        help="skip the network checks (Binance, keys, heartbeat ping); the bot retries those",
    )

    command("notify", "set up Telegram alerts (guided) and send a test message")

    dashboard = session("dashboard", "write an HTML dashboard of a session and open it")
    dashboard.add_argument("--data-dir", type=Path, default=Path("data"))
    dashboard.add_argument("--out", default=None, help="a file or folder (default: reports/)")
    dashboard.add_argument("--no-open", action="store_true", help="do not open it in a browser")

    compare = session("compare", "compare a session with a backtest of the same period")
    compare.add_argument("--data-dir", type=Path, default=Path("data"))

    backup = session("backup", "copy a session ledger, also while it runs")
    backup.add_argument(
        "--out", default=None, help="a file or folder (default: backups/ by the ledger)"
    )

    session("resume", "clear the kill switch of a halted session", "the halted one")
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
            type=Timeframe,
            default=DEFAULT_TIMEFRAMES,
            help="e.g. 1h 4h 1d (default: 1h 4h)",
        )
        parser_.add_argument(
            "--data-dir", type=Path, default=Path("data"), help="bar store (default: data)"
        )

    backtest = command("backtest", "run a backtest from a YAML config")
    backtest.add_argument("config", type=config_arg, help=CONFIG_HELP)
    backtest.add_argument("--data-dir", type=Path, default=Path("data"))
    backtest.add_argument(
        "--attribution", action="store_true", help="also run each strategy alone and compare"
    )
    backtest.add_argument(
        "--html", default=None, help="write an HTML dashboard to this file or folder"
    )
    backtest.add_argument("--no-open", action="store_true", help="do not open the HTML file")

    validate = command("validate", "run the validation pipeline")
    validate.add_argument(
        "config",
        type=config_arg,
        help="a validation config, or its name from config/ such as donchian_voltarget_validation",
    )
    validate.add_argument("--data-dir", type=Path, default=Path("data"))
    validate.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    return parser


def run_download(args: argparse.Namespace) -> int:
    store = BarStore(args.data_dir)
    with httpx.Client(timeout=30.0) as client:
        clock = ServerClock(client)  # a fast local clock would store a bar still open
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


def print_report(symbol: str, timeframe: Timeframe, report: QualityReport) -> None:
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
    if report.gaps:
        print(f"  gaps: {len(report.gaps)} ({report.missing_bars} missing bars)")
        for gap in report.gaps[:MAX_LISTED]:
            print(f"    {fmt(gap.start)} .. {fmt(gap.end)} ({gap.missing})")
    if report.zero_volume:
        print(f"  zero volume bars: {report.zero_volume}")
    if report.large_moves:
        listed = ", ".join(fmt(moment) for moment in report.large_moves[:MAX_LISTED])
        print(f"  large moves: {len(report.large_moves)} ({listed})")


def run_check(args: argparse.Namespace) -> int:
    store = BarStore(args.data_dir)
    now = datetime.now(UTC)
    failed = False
    for symbol in args.symbols:
        for timeframe in args.timeframes:
            bars = store.read(symbol, timeframe)
            if bars.is_empty():
                print(f"{symbol} {timeframe}: FAIL, no data")
                failed = True
                continue
            report = check_bars(bars, timeframe, now)
            print_report(symbol, timeframe, report)
            failed = failed or not report.ok
    return 1 if failed else 0


def trial_log(data_dir: Path) -> TrialLog:
    return TrialLog(data_dir / "trials.jsonl")


def print_notes(config: TradingConfig, store: BarStore) -> None:
    """What a backtest of this config cannot show, said before it runs."""
    ends = data_ends(config, store)
    if len(set(ends.values())) > 1:
        (symbol, timeframe), first = min(ends.items(), key=lambda item: item[1])
        print(
            f"Note: {symbol} {timeframe} data ends {first:%Y-%m-%d %H:%M}; every stream stops "
            "there (`tbot download` brings it up to date)"
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
            raise ValueError(f"{stored}, before the period starts: run `tbot download`")
        if config.end is not None and utc_day(config.end) <= first:
            raise ValueError(f"{stored}, after the period ends: no download goes back further")


def utc_day(day: date) -> datetime:
    return datetime.combine(day, time(), tzinfo=UTC)


def output_file(value: str | None, folder: Path, name: str) -> Path:
    """Where to write: the file named, or `name` in a folder (one that exists, or a path
    ending in a slash), or `name` in folder when nothing is named."""
    if value is None:
        return folder / name
    path = Path(value)
    return path / name if path.is_dir() or value.endswith(("/", "\\")) else path


def run_backtest_command(args: argparse.Namespace) -> int:
    config = load(args.config, load_config)
    check_strategies(args.config, config)
    check_period(config, BarStore(args.data_dir))
    print_notes(config, BarStore(args.data_dir))
    result = run_backtest(config, BarStore(args.data_dir))
    metrics = compute_metrics(result)
    trial_log(args.data_dir).append([make_record(config, metrics, "backtest")])
    print(format_metrics(metrics))
    for symbol, quantity in sorted(result.positions.items()):
        print(f"Open position {symbol} {quantity:.6f}")
    if result.halted:
        print(f"Kill switch  {result.halted}; flat from then on")
    if args.attribution:
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
    if args.html:
        subtitle = f"{args.config}, {config.start} to {config.end or 'latest'}"
        data = from_backtest(result, f"Backtest {args.config.stem}", subtitle)
        out = output_file(args.html, Path("reports"), f"{args.config.stem}.html")
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
    config = existing_ledger(load_session_config(path))
    out = output_file(args.out, Path("reports"), f"{path.stem}.html")
    kind = config.mode.capitalize() if isinstance(config, LiveConfig) else "Paper"
    title = f"{kind} session {path.stem}"
    write_dashboard(from_ledger(config, BarStore(args.data_dir), title), out, show=not args.no_open)
    return 0


def run_validate_command(args: argparse.Namespace) -> int:
    config, base = load(args.config, load_validation_config)
    check_period(base, BarStore(args.data_dir))
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
    not_running(config)
    log_file = args.log_file or log_path(args.config)
    announce("paper", args.config, config, log_file)
    configure_logging(log_file)
    return asyncio.run(run_paper(config, Settings(), args.data_dir))


def run_live_command(args: argparse.Namespace) -> int:
    config = load_live_config(args.config)
    if config.mode == "live" and not args.live:
        raise ConfigError("config mode is live: pass --live to trade real money")
    if config.mode != "live" and args.live:
        raise ConfigError("--live is only for mode: live; this config trades on the testnet")
    check_strategies(args.config, config)
    settings = Settings()
    if not settings.binance_api_key or not settings.binance_api_secret:
        raise ConfigError(KEYS_MISSING)  # every start would fail the same way
    not_running(config)
    log_file = args.log_file or log_path(args.config)
    announce(config.mode, args.config, config, log_file)
    configure_logging(log_file)
    return asyncio.run(run_live(config, settings, args.data_dir, confirmed=args.live))


def not_running(config: SessionConfig) -> None:
    if config.ledger.is_file() and is_running(config.ledger):
        raise AlreadyRunning(f"a session already runs on {config.ledger}: `tbot status` shows it")


def announce(kind: str, path: Path, config: SessionConfig, log_file: Path) -> None:
    """What runs and how to stop it, before the log takes over the console."""
    name = short_name(path)
    print(
        f"{kind} session {name}: ledger {config.ledger}, log {log_file}\n"
        f"stop it with Ctrl+C here, or `tbot stop {name}` from another terminal",
        file=sys.stderr,
        flush=True,
    )


def log_path(config: Path) -> Path:
    """One log per config, so two sessions never rotate the same file."""
    return Path("logs") / f"{config.stem}.jsonl"


def run_status_command(args: argparse.Namespace) -> int:
    store = BarStore(args.data_dir)
    if args.config is None:
        sessions, broken = scan_configs()
        rows = [session_row(path.stem, config, store) for path, config in sessions]
        rows += [broken_row(b.path.stem, b.problem, b.ledger) for b in broken]
        print(overview_text(rows))
        return 0
    print(status_text(existing_ledger(load_session_config(args.config)), store))
    return 0


def run_log_command(args: argparse.Namespace) -> int:
    if args.file is None and args.config is not None:
        read_yaml(args.config)  # a typo is no config, not a log yet to come
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
    config = load_session_config(args.config or pick_config("doctor"))

    async def check() -> list[Check]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            store = BarStore(args.data_dir)
            return await run_checks(config, Settings(), client, store, offline=args.offline)

    checks = asyncio.run(check())
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
            return 0
    print(f"still running: `tbot log {short_name(path)}` shows what it is doing", file=sys.stderr)
    return EXIT_ERROR


def run_compare_command(args: argparse.Namespace) -> int:
    config = existing_ledger(load_session_config(args.config or pick_config("compare")))
    with Ledger(config.ledger) as ledger:
        try:
            comparison = compare_session(config, ledger, BarStore(args.data_dir))
        except ValueError as exc:
            print(f"cannot compare: {exc}", file=sys.stderr)
            return EXIT_ERROR
    print(comparison_text(comparison, str(config.ledger)))
    return 0 if not comparison.checks() else 1


def run_backup_command(args: argparse.Namespace) -> int:
    config = existing_ledger(load_session_config(args.config or pick_config("backup")))
    name = f"{config.ledger.stem}-{datetime.now(UTC):%Y%m%d-%H%M%S}.sqlite"
    out = output_file(args.out, backup_dir(config.ledger), name)  # a folder: a synced one
    with Ledger(config.ledger) as ledger:
        ledger.backup(out)
    print(f"wrote {out}")
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
    print(resume(existing_ledger(load_session_config(path))))
    return 0


def is_halted(config: SessionConfig) -> bool:
    try:
        with Ledger(config.ledger) as ledger:
            return load_guard(config, ledger).state.halted
    except Exception:  # an unusable ledger is reported when it is named
        return False


def existing_ledger[C: SessionConfig](config: C) -> C:
    """Opening a ledger creates it; commands that only read must not leave one behind."""
    if not config.ledger.is_file():
        start = "tbot live <config>" if isinstance(config, LiveConfig) else "tbot paper"
        raise ConfigError(
            f"no ledger at {config.ledger}: nothing has run with this config yet "
            f"(`{start}` starts it; `tbot status` lists every session)"
        )
    return config


def run_account_command(args: argparse.Namespace) -> int:
    config = load_live_config(args.config or pick_config("account", live=True))
    print(asyncio.run(account_text(config, Settings())))
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
    return asyncio.run(set_up_telegram(token, chat_id, new_token, Path(".env")))


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
            chat_id = await find_chat(bot, name)
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


async def find_chat(bot: Telegram, name: str) -> str | None:
    """The chat that wrote to the bot; waits for a first message when someone can send it."""
    try:
        chats = await chats_of(bot, 0)
        if not chats and interactive():
            print(f"Now send any message to @{name} in Telegram. Waiting up to 2 minutes...")
            deadline = monotonic() + CHAT_WAIT_SECONDS
            while not chats and monotonic() < deadline:
                chats = await chats_of(bot, 10)  # long poll: answers as soon as one arrives
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
    if not chats:
        print(f"no message to @{name} yet: send it any message, then run `tbot notify` again")
        return None
    ids = list(chats)
    if len(ids) == 1:
        print(f"found chat {ids[0]} {chats[ids[0]]}".rstrip())
        if not interactive():
            return ids[0]
        answer = input("Send the alerts to this chat? [Y/n] ").strip().lower()
        if answer in ("", "y", "yes"):
            return ids[0]
        print("not saved: send the bot a message from the chat you want, then run it again")
        return None
    for number, chat in enumerate(ids, 1):
        print(f"{number}. chat {chat} {chats[chat]}".rstrip())
    if interactive():
        answer = input(f"Which one gets the alerts (1-{len(ids)})? ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(ids):
            return ids[int(answer) - 1]
    print("put the chat id in TBOT_TELEGRAM_CHAT_ID in .env, then run `tbot notify` again")
    return None


async def chats_of(bot: Telegram, timeout: int) -> dict[str, str]:
    """Chat id to name for every chat that wrote to the bot lately."""
    chats: dict[str, str] = {}
    for update in await bot.updates(None, timeout):
        chat = (update.get("message") or {}).get("chat") or {}
        if "id" in chat:
            name = chat.get("title") or chat.get("first_name") or chat.get("username") or ""
            chats[str(chat["id"])] = str(name)
    return chats


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
    "resume": run_resume_command,
    "account": run_account_command,
    "dashboard": run_dashboard_command,
    "notify": run_notify_command,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse: 0 after --help, 2 for a wrong command line
        return EXIT_CONFIG if exc.code == 2 else int(exc.code or 0)
    if args.command not in COMMANDS:
        parser.print_help()
        return 0
    session = args.command in ("paper", "live")
    try:
        return COMMANDS[args.command](args)
    except ConfigError as exc:
        return _fail(str(exc.code), EXIT_CONFIG, alert=session)
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
        if os.environ.get("TBOT_DEBUG"):
            raise
        return _fail(f"error: {brief(exc)}", EXIT_ERROR, alert=False)


def _fail(message: str, code: int, *, alert: bool) -> int:
    print(message, file=sys.stderr)
    if alert:  # a supervised bot that cannot start says so before the supervisor gives up
        _alert(f"tbot cannot start: {message}")
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
