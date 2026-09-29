from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from factories import price_bars
from tbot.backtest.engine import BacktestResult
from tbot.cli import main
from tbot.core.config import StrategyConfig
from tbot.core.models import Fill
from tbot.core.timeframe import Timeframe
from tbot.data.store import BarStore
from tbot.live.config import PaperConfig
from tbot.live.ledger import EquityPoint, Ledger
from tbot.monitoring.dashboard import (
    DashboardData,
    downsample,
    drawdowns,
    from_backtest,
    from_ledger,
    nice_ticks,
    render,
)

T0 = datetime(2024, 1, 1, tzinfo=UTC)
H4 = Timeframe.H4


def test_drawdowns_from_running_peak() -> None:
    assert drawdowns([110, 99, 121, 100], 100) == pytest.approx([0.0, -0.1, 0.0, 100 / 121 - 1])
    assert drawdowns([90], 100) == pytest.approx([-0.1])  # the initial equity counts as a peak


def test_nice_ticks() -> None:
    assert nice_ticks(0, 100) == [0, 50, 100]
    assert nice_ticks(9800, 12600) == [9000, 10000, 11000, 12000, 13000]
    assert nice_ticks(-0.31, 0) == pytest.approx([-0.4, -0.3, -0.2, -0.1, 0.0])
    assert nice_ticks(5, 5)[0] <= 5 <= nice_ticks(5, 5)[-1]


def test_downsample_keeps_the_last_point() -> None:
    values = list(range(10_000))
    kept = downsample(values, limit=1000)
    assert len(kept) <= 1001
    assert kept[0] == 0
    assert kept[-1] == 9999
    assert downsample([1, 2, 3], limit=1000) == [1, 2, 3]


def sample_data() -> DashboardData:
    times = [T0 + timedelta(hours=4 * i) for i in range(50)]
    equity = [1000 + 10 * i - (60 if 20 <= i < 30 else 0) for i in range(50)]
    return DashboardData(
        title="Paper session",
        subtitle="ledger x",
        times=times,
        equity=[float(v) for v in equity],
        initial=1000.0,
        positions=[("BTCUSDT", 0.01, 50000.0)],
        fills=[Fill(T0, "BTCUSDT", 0.01, 50000.0, 0.5)],
        notes=["HALTED: test"],
    )


def test_render_contains_charts_tiles_and_tables() -> None:
    page = render(sample_data())
    assert page.startswith("<!DOCTYPE html>")
    assert '<svg class="chart" id="equity"' in page
    assert '<svg class="chart" id="drawdown"' in page
    assert page.count('class="line"') == 2
    assert "Total return" in page
    assert "+49.0%" in page  # 1000 -> 1490
    assert "BUY" in page
    assert "HALTED: test" in page
    assert "prefers-color-scheme: dark" in page
    assert '"times":' in page  # series for the crosshair tooltip
    # No path point leaves the plot area.
    import re

    for match in re.finditer(r'class="line" d="M([^"]+)"', page):
        for pair in match.group(1).split(" L"):
            x, y = (float(v) for v in pair.split(","))
            assert 0 <= x <= 880
            assert 0 <= y <= 260


def test_render_handles_empty_data() -> None:
    page = render(DashboardData("t", "s", [], [], 1000.0, []))
    assert "no data" in page
    assert "no open positions" in page


def test_from_ledger_and_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = PaperConfig(
        initial_cash=1000.0,
        strategies=[
            StrategyConfig(name="donchian_trend", symbols=["BTCUSDT"], timeframe=H4, allocation=1)
        ],
        ledger=tmp_path / "paper.sqlite",
    )
    ledger = Ledger(config.ledger)
    ledger.add_fill(Fill(T0, "BTCUSDT", 0.01, 50000.0, 0.5), 50000.0)
    ledger.add_equity(EquityPoint(T0, 1000.0, 499.5, 0.5))
    ledger.add_equity(EquityPoint(T0 + timedelta(hours=4), 1010.0, 499.5, 0.5))
    ledger.add_event(T0, "info", "started")
    ledger.close()
    store = BarStore(tmp_path / "data")
    store.write("BTCUSDT", H4, price_bars(T0, H4, [50000.0, 51000.0], [51000.0, 52000.0]))

    data = from_ledger(config, store, "Paper")
    assert data.equity == [1000.0, 1010.0]
    [(symbol, quantity, mark)] = data.positions
    assert (symbol, mark) == ("BTCUSDT", 52000.0)
    assert quantity == pytest.approx(0.01)
    assert [e.message for e in data.events] == ["started"]

    config_path = tmp_path / "paper.yaml"
    config_path.write_text(
        "strategies:\n"
        "  - {name: donchian_trend, symbols: [BTCUSDT], timeframe: 4h, allocation: 1.0}\n"
        f"ledger: {config.ledger.as_posix()}\n",
        encoding="utf-8",
    )
    out = tmp_path / "dash.html"
    code = main(
        ["dashboard", str(config_path), "--data-dir", str(tmp_path / "data"), "--out", str(out)]
    )
    assert code == 0
    assert str(out) in capsys.readouterr().out
    assert "Equity" in out.read_text(encoding="utf-8")


def test_from_backtest() -> None:
    times = [T0 + timedelta(hours=i) for i in range(3)]
    result = BacktestResult(
        initial_cash=100.0,
        equity=pl.DataFrame(
            {
                "time": times,
                "equity": [100.0, 110.0, 105.0],
                "cash": [0.0] * 3,
                "exposure": [1.0] * 3,
            }
        ),
        fills=pl.DataFrame(
            {
                "time": [times[0]],
                "symbol": ["BTC"],
                "quantity": [1.0],
                "price": [100.0],
                "fee": [0.1],
            }
        ),
        trades=pl.DataFrame(),
        positions={"BTC": 1.0},
    )
    data = from_backtest(result, "Backtest", "config x")
    assert data.positions == [("BTC", 1.0, 100.0)]
    assert len(data.fills) == 1
    assert "Backtest" in render(data)
