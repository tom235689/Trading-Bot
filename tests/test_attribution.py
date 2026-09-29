from datetime import UTC, datetime
from pathlib import Path

import pytest

from factories import price_bars
from tbot.backtest.attribution import format_attribution, run_attribution
from tbot.backtest.config import BacktestConfig
from tbot.cli import main
from tbot.core.timeframe import Timeframe
from tbot.data.store import BarStore

H1 = Timeframe.H1
T0 = datetime(2024, 1, 1, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> BarStore:
    """Twelve weeks of hourly bars cycling up 1% for 40 bars, then down 1% for 40 bars."""
    closes = [100.0]
    for i in range(1, 2000):
        closes.append(closes[-1] * (1.01 if (i // 40) % 2 == 0 else 0.99))
    store = BarStore(tmp_path / "data")
    store.write("BTCUSDT", H1, price_bars(T0, H1, [closes[0], *closes[:-1]], closes))
    return store


CONFIG = {
    "start": "2024-01-15",
    "initial_cash": 1000,
    "strategies": [
        {
            "name": "donchian_trend",
            "symbols": ["BTCUSDT"],
            "timeframe": "1h",
            "allocation": 0.5,
            "params": {"entry": 10, "exit": 5},
        },
        {
            "name": "rsi_reversion",
            "symbols": ["BTCUSDT"],
            "timeframe": "1h",
            "allocation": 0.5,
            "params": {"period": 5, "low": 30, "high": 70},
        },
    ],
}


def test_attribution_runs_each_strategy_and_the_mix(store: BarStore) -> None:
    attribution = run_attribution(BacktestConfig.model_validate(CONFIG), store)
    labels = [row.label for row in attribution.rows]
    assert labels == ["donchian_trend", "rsi_reversion", "combined"]
    assert [row.allocation for row in attribution.rows] == [0.5, 0.5, 1.0]
    assert all(row.metrics.trades > 0 for row in attribution.rows)
    assert attribution.correlation[0][0] == pytest.approx(1.0)
    assert -1.0 <= attribution.correlation[0][1] <= 1.0
    text = format_attribution(attribution)
    assert "daily return correlation" in text
    assert "combined" in text


def test_backtest_command_with_attribution(
    tmp_path: Path, store: BarStore, capsys: pytest.CaptureFixture[str]
) -> None:
    import yaml

    path = tmp_path / "multi.yaml"
    path.write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    code = main(["backtest", str(path), "--data-dir", str(tmp_path / "data"), "--attribution"])
    out = capsys.readouterr().out
    assert code == 0
    assert "rsi_reversion" in out
    assert "combined" in out
