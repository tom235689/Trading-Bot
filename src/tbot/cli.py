"""Command line entry point."""

import argparse
import asyncio
import os
from datetime import UTC, date, datetime, time
from pathlib import Path

import httpx
import yaml

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
from tbot.live.config import LiveConfig, PaperConfig, SessionConfig, Settings, load_live_config
from tbot.live.runner import account_text, resume, run_live, run_paper, status_text
from tbot.monitoring.dashboard import DashboardData, from_backtest, from_ledger, render
from tbot.monitoring.logging import configure_logging
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


def load_session_config(path: Path) -> SessionConfig:
    """A config with `mode` is a live config; anything else is paper."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if "mode" in raw:
        return LiveConfig.model_validate(raw)
    return PaperConfig.model_validate(raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tbot", description="Multi-strategy crypto trading bot.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")

    download = commands.add_parser("download", help="download closed bars from Binance")
    download.add_argument("--start", type=parse_date, default=parse_date(DEFAULT_START))
    check = commands.add_parser("check", help="check stored bars")
    for command in (download, check):
        command.add_argument("--symbols", nargs="+", type=parse_symbol, default=DEFAULT_SYMBOLS)
        command.add_argument("--timeframes", nargs="+", type=Timeframe, default=DEFAULT_TIMEFRAMES)
        command.add_argument("--data-dir", type=Path, default=Path("data"))

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
    live.add_argument("--log-file", type=Path, default=Path("logs/live.jsonl"))

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
    config = load_config(args.config)
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
    config = load_session_config(args.config)
    out = args.out or Path("reports") / f"{args.config.stem}.html"
    title = f"{'Live' if isinstance(config, LiveConfig) else 'Paper'} session {args.config.stem}"
    write_dashboard(from_ledger(config, BarStore(args.data_dir), title), out)
    return 0


def run_validate_command(args: argparse.Namespace) -> int:
    config, base = load_validation_config(args.config)
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
    configure_logging(args.log_file)
    config = load_session_config(args.config)
    if not isinstance(config, PaperConfig):
        raise SystemExit("this is a live config; use `tbot live`")
    return asyncio.run(run_paper(config, Settings(), args.data_dir))


def run_live_command(args: argparse.Namespace) -> int:
    configure_logging(args.log_file)
    config = load_live_config(args.config)
    return asyncio.run(run_live(config, Settings(), args.data_dir, confirmed=args.live))


def run_status_command(args: argparse.Namespace) -> int:
    print(status_text(load_session_config(args.config), BarStore(args.data_dir)))
    return 0


def run_resume_command(args: argparse.Namespace) -> int:
    print(resume(load_session_config(args.config)))
    return 0


def run_account_command(args: argparse.Namespace) -> int:
    print(asyncio.run(account_text(load_live_config(args.config), Settings())))
    return 0


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
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in COMMANDS:
        return COMMANDS[args.command](args)
    parser.print_help()
    return 0
