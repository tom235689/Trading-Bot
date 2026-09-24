"""Command line entry point."""

import argparse
from datetime import UTC, date, datetime, time
from pathlib import Path

import httpx

from tbot import __version__
from tbot.backtest.config import load_config
from tbot.backtest.metrics import compute_metrics
from tbot.backtest.report import format_metrics
from tbot.backtest.runner import run_backtest
from tbot.core.timeframe import Timeframe
from tbot.data.downloader import sync
from tbot.data.quality import QualityReport, check_bars
from tbot.data.store import BarStore

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


def run_backtest_command(args: argparse.Namespace) -> int:
    result = run_backtest(load_config(args.config), BarStore(args.data_dir))
    print(format_metrics(compute_metrics(result)))
    for symbol, quantity in sorted(result.positions.items()):
        print(f"Open position {symbol} {quantity:.6f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "download":
        return run_download(args)
    if args.command == "check":
        return run_check(args)
    if args.command == "backtest":
        return run_backtest_command(args)
    parser.print_help()
    return 0
