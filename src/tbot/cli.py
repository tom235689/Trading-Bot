"""Command line entry point."""

import argparse
import asyncio
import os
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import ValidationError

from tbot import __version__
from tbot.backtest.attribution import format_attribution, run_attribution
from tbot.backtest.config import load_config
from tbot.backtest.metrics import compute_metrics
from tbot.backtest.report import format_metrics
from tbot.backtest.runner import run_backtest
from tbot.core.timeframe import Timeframe
from tbot.data.downloader import sync
from tbot.data.quality import QualityReport, check_bars
from tbot.data.store import BarStore
from tbot.live.config import LiveConfig, PaperConfig, SessionConfig, Settings
from tbot.live.ledger import LedgerUnavailable
from tbot.live.runner import AlreadyRunning, account_text, resume, run_live, run_paper, status_text
from tbot.monitoring.dashboard import DashboardData, from_backtest, from_ledger, render
from tbot.monitoring.logging import configure_logging
from tbot.monitoring.telegram import Telegram
from tbot.research.report import format_report
from tbot.research.trials import TrialLog, make_record
from tbot.research.validate import load_validation_config, run_validation

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT"]
DEFAULT_TIMEFRAMES = [Timeframe.H1, Timeframe.H4]
DEFAULT_START = "2017-08-01"
MAX_LISTED = 10


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
        return "a testnet or live config (tbot live, account, status, resume, dashboard)"
    if "ledger" in raw:
        return "a paper config (tbot paper, status, resume, dashboard)"
    if "start" in raw:
        return "a backtest config (tbot backtest)"
    return "not a tbot config"


def read_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise SystemExit(f"{path}: cannot read ({exc.strerror})") from None
    except yaml.YAMLError as exc:
        raise SystemExit(f"{path}: not valid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise SystemExit(f"{path}: expected a mapping of settings")
    return raw


def load[T](path: Path, loader: Callable[[Path], T]) -> T:
    """Run a config loader; a bad file ends the command with a readable message."""
    raw = read_yaml(path)
    try:
        return loader(path)
    except (ValidationError, ValueError) as exc:
        raise SystemExit(f"{path}: invalid for this command ({config_kind(raw)}):\n{exc}") from None
    except OSError as exc:  # a file the config points at, such as a validation's backtest
        raise SystemExit(f"{path}: {exc}") from None


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tbot", description="Multi-strategy crypto trading bot.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")

    download = commands.add_parser("download", help="download closed bars from Binance")
    download.add_argument(
        "--start",
        type=parse_date,
        default=parse_date(DEFAULT_START),
        help=f"first day, YYYY-MM-DD (default {DEFAULT_START}); earlier history is backfilled",
    )
    check = commands.add_parser("check", help="check stored bars")
    for command in (download, check):
        command.add_argument(
            "--symbols",
            nargs="+",
            type=parse_symbol,
            default=DEFAULT_SYMBOLS,
            help="e.g. BTCUSDT ETH/USDT (default: %(default)s)",
        )
        command.add_argument(
            "--timeframes",
            nargs="+",
            type=Timeframe,
            default=DEFAULT_TIMEFRAMES,
            help="e.g. 1h 4h 1d (default: 1h 4h)",
        )
        command.add_argument(
            "--data-dir", type=Path, default=Path("data"), help="bar store (default: data)"
        )

    backtest = commands.add_parser("backtest", help="run a backtest from a YAML config")
    backtest.add_argument("config", type=Path)
    backtest.add_argument("--data-dir", type=Path, default=Path("data"))
    backtest.add_argument(
        "--attribution", action="store_true", help="also run each strategy alone and compare"
    )
    backtest.add_argument("--html", type=Path, default=None, help="write an HTML dashboard")

    validate = commands.add_parser("validate", help="run the validation pipeline")
    validate.add_argument("config", type=Path)
    validate.add_argument("--data-dir", type=Path, default=Path("data"))
    validate.add_argument("--workers", type=int, default=os.cpu_count() or 1)

    paper = commands.add_parser("paper", help="paper trade on live Binance data until stopped")
    paper.add_argument("config", type=Path)
    paper.add_argument("--data-dir", type=Path, default=Path("data"))
    paper.add_argument("--log-file", type=Path, default=Path("logs/paper.jsonl"))

    live = commands.add_parser("live", help="trade on Binance testnet or live until stopped")
    live.add_argument("config", type=Path)
    live.add_argument("--live", action="store_true", help="required when the config mode is live")
    live.add_argument("--data-dir", type=Path, default=Path("data"))
    live.add_argument("--log-file", type=Path, help="default: logs/<mode>.jsonl")

    status = commands.add_parser("status", help="show a session ledger")
    status.add_argument("config", type=Path)
    status.add_argument("--data-dir", type=Path, default=Path("data"))

    resume_cmd = commands.add_parser("resume", help="clear the kill switch of a session")
    resume_cmd.add_argument("config", type=Path)

    account = commands.add_parser("account", help="show exchange balances and open orders")
    account.add_argument("config", type=Path)

    dashboard = commands.add_parser("dashboard", help="write an HTML dashboard of a session")
    dashboard.add_argument("config", type=Path)
    dashboard.add_argument("--data-dir", type=Path, default=Path("data"))
    dashboard.add_argument("--out", type=Path, default=None, help="default reports/<name>.html")

    commands.add_parser("notify", help="send a test Telegram alert with the .env settings")
    return parser


def run_download(args: argparse.Namespace) -> int:
    store = BarStore(args.data_dir)
    now = datetime.now(UTC)
    with httpx.Client(timeout=30.0) as client:
        for symbol in args.symbols:
            for timeframe in args.timeframes:
                result = sync(store, client, symbol, timeframe, args.start, now, progress=print)
                print(
                    f"{symbol} {timeframe}: {result.total_bars} bars stored, "
                    f"{fmt(result.first)} -> {fmt(result.last)}"
                )
                if result.dropped:
                    print(f"  dropped {result.dropped} misaligned bars")
    return 0


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
        "invalid prices": report.invalid_prices,
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


def run_backtest_command(args: argparse.Namespace) -> int:
    config = load(args.config, load_config)
    result = run_backtest(config, BarStore(args.data_dir))
    metrics = compute_metrics(result)
    trial_log(args.data_dir).append([make_record(config, metrics, "backtest")])
    print(format_metrics(metrics))
    for symbol, quantity in sorted(result.positions.items()):
        print(f"Open position {symbol} {quantity:.6f}")
    if args.attribution:
        print()
        print(format_attribution(run_attribution(config, BarStore(args.data_dir))))
    if args.html:
        subtitle = f"{args.config}, {config.start} to {config.end or 'latest'}"
        write_dashboard(from_backtest(result, f"Backtest {args.config.stem}", subtitle), args.html)
    return 0


def write_dashboard(data: DashboardData, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(data), encoding="utf-8")
    print(f"wrote {out}")


def run_dashboard_command(args: argparse.Namespace) -> int:
    config = existing_ledger(load_session_config(args.config))
    out = args.out or Path("reports") / f"{args.config.stem}.html"
    title = f"{'Live' if isinstance(config, LiveConfig) else 'Paper'} session {args.config.stem}"
    write_dashboard(from_ledger(config, BarStore(args.data_dir), title), out)
    return 0


def run_validate_command(args: argparse.Namespace) -> int:
    config, base = load(args.config, load_validation_config)
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
        raise SystemExit("this is a live config; use `tbot live`")
    configure_logging(args.log_file)
    return asyncio.run(run_paper(config, Settings(), args.data_dir))


def run_live_command(args: argparse.Namespace) -> int:
    config = load_live_config(args.config)
    if config.mode == "live" and not args.live:
        raise SystemExit("config mode is live: pass --live to trade real money")
    if config.mode != "live" and args.live:
        raise SystemExit("--live is only for mode: live; this config trades on the testnet")
    configure_logging(args.log_file or Path("logs") / f"{config.mode}.jsonl")
    return asyncio.run(run_live(config, Settings(), args.data_dir, confirmed=args.live))


def run_status_command(args: argparse.Namespace) -> int:
    print(status_text(existing_ledger(load_session_config(args.config)), BarStore(args.data_dir)))
    return 0


def run_resume_command(args: argparse.Namespace) -> int:
    print(resume(existing_ledger(load_session_config(args.config))))
    return 0


def existing_ledger[C: SessionConfig](config: C) -> C:
    """Opening a ledger creates it; commands that only read must not leave one behind."""
    if not config.ledger.is_file():
        raise SystemExit(f"no ledger at {config.ledger}: nothing has run with this config yet")
    return config


def run_account_command(args: argparse.Namespace) -> int:
    print(asyncio.run(account_text(load_live_config(args.config), Settings())))
    return 0


def run_notify_command(args: argparse.Namespace) -> int:
    settings = Settings()
    if not settings.telegram_token or not settings.telegram_chat_id:
        print("set TBOT_TELEGRAM_TOKEN and TBOT_TELEGRAM_CHAT_ID in .env first")
        return 1
    token, chat_id = settings.telegram_token, settings.telegram_chat_id

    async def send() -> bool:
        async with httpx.AsyncClient() as client:
            return await Telegram(token, chat_id, client).send("tbot: test alert")

    if asyncio.run(send()):
        print("sent: check the Telegram chat")
        return 0
    print("failed: see the log line above; check the token and the chat id")
    return 1


COMMANDS = {
    "download": run_download,
    "check": run_check,
    "backtest": run_backtest_command,
    "validate": run_validate_command,
    "paper": run_paper_command,
    "live": run_live_command,
    "status": run_status_command,
    "resume": run_resume_command,
    "account": run_account_command,
    "dashboard": run_dashboard_command,
    "notify": run_notify_command,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in COMMANDS:
        try:
            return COMMANDS[args.command](args)
        except (LedgerUnavailable, AlreadyRunning) as exc:
            print(exc, file=sys.stderr)
            return 2
    parser.print_help()
    return 0
