from datetime import UTC, date, datetime
from pathlib import Path

import pydantic
import pytest

from factories import price_bars
from tbot.backtest.config import BacktestConfig, load_config
from tbot.backtest.runner import run_backtest
from tbot.core.timeframe import Timeframe
from tbot.data.store import BarStore

H4 = Timeframe.H4
T0 = datetime(2024, 1, 1, tzinfo=UTC)

CONFIG = """
start: 2024-01-05
initial_cash: 1000
costs: {fee_rate: 0.001, slippage_bps: 5}
strategies:
  - name: donchian_trend
    symbols: [btc/usdt]
    timeframe: 4h
    allocation: 1.0
    params: {entry: 5, exit: 3}
"""


def write_config(tmp_path: Path, text: str = CONFIG) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_config_normalizes_and_defaults(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    assert config.start == date(2024, 1, 5)
    assert config.strategies[0].symbols == ["BTCUSDT"]
    assert config.strategies[0].timeframe is H4
    assert config.rebalance.min_notional == 10
    assert config.risk.long_only


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"end": "2024-01-01"}, "end must be after start"),
        ({"extra": 1}, "Extra inputs"),
    ],
)
def test_config_rejects_invalid(tmp_path: Path, change: dict[str, object], message: str) -> None:
    raw = load_config(write_config(tmp_path)).model_dump() | change
    with pytest.raises(pydantic.ValidationError, match=message):
        BacktestConfig.model_validate(raw)


def test_config_rejects_over_allocation(tmp_path: Path) -> None:
    raw = load_config(write_config(tmp_path)).model_dump()
    raw["strategies"] = raw["strategies"] * 2
    with pytest.raises(pydantic.ValidationError, match="sum to at most 1"):
        BacktestConfig.model_validate(raw)


def test_runs_trend_on_stored_bars(tmp_path: Path) -> None:
    # 30 flat bars, 20 rising 2% per bar, then 20 falling 2% per bar.
    closes = [100.0] * 30
    for step in [1.02] * 20 + [0.98] * 20:
        closes.append(closes[-1] * step)
    store = BarStore(tmp_path / "data")
    store.write("BTCUSDT", H4, price_bars(T0, H4, [closes[0], *closes[:-1]], closes))

    result = run_backtest(load_config(write_config(tmp_path)), store)

    [trade] = result.trades.rows(named=True)
    assert trade["pnl"] > 0
    assert result.equity["time"][0] >= datetime(2024, 1, 5, tzinfo=UTC)
    assert result.equity["equity"][-1] > 1000


def test_missing_data_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="run `tbot download`"):
        run_backtest(load_config(write_config(tmp_path)), BarStore(tmp_path / "empty"))
