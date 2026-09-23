from datetime import UTC, datetime
from pathlib import Path

import pytest

from binance_fake import make_bars
from tbot import __version__
from tbot.cli import build_parser, main, parse_symbol
from tbot.core.timeframe import Timeframe
from tbot.data.store import BarStore


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


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
