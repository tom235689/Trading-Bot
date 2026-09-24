import math
from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from tbot.backtest.engine import BacktestResult
from tbot.backtest.metrics import compute_metrics
from tbot.backtest.report import format_metrics

T0 = datetime(2024, 1, 1, tzinfo=UTC)


def day(n: int) -> datetime:
    return T0 + timedelta(days=n)


def result() -> BacktestResult:
    return BacktestResult(
        initial_cash=100.0,
        equity=pl.DataFrame(
            {
                "time": [day(1), day(2), day(3)],
                "equity": [110.0, 99.0, 118.8],
                "cash": [0.0, 0.0, 0.0],
                "exposure": [0.5, 1.0, 0.0],
            }
        ),
        fills=pl.DataFrame({"quantity": [1.0], "price": [100.0], "fee": [0.1]}),
        trades=pl.DataFrame({"pnl": [10.0, -5.0, 20.0], "return": [0.1, -0.05, 0.2]}),
        positions={},
    )


def test_metrics_match_hand_calculation() -> None:
    m = compute_metrics(result())
    # Daily returns +10%, -10%, +20% from 100 -> 110 -> 99 -> 118.8.
    mean = 0.2 / 3
    std = math.sqrt(((0.1 - mean) ** 2 + (-0.1 - mean) ** 2 + (0.2 - mean) ** 2) / 2)
    downside = math.sqrt(0.01 / 3)
    years = 2 / 365.25

    assert m.years == pytest.approx(years)
    assert m.total_return == pytest.approx(0.188)
    assert m.cagr == pytest.approx(1.188 ** (1 / years) - 1)
    assert m.sharpe == pytest.approx(mean / std * math.sqrt(365))
    assert m.sortino == pytest.approx(mean / downside * math.sqrt(365))
    assert m.max_drawdown == pytest.approx(-0.1)
    assert m.calmar == pytest.approx(m.cagr / 0.1)
    assert m.avg_exposure == pytest.approx(0.5)
    assert m.turnover == pytest.approx(100 / ((110 + 99 + 118.8) / 3) / years)
    assert m.fees == pytest.approx(0.1)
    assert m.trades == 3
    assert m.win_rate == pytest.approx(2 / 3)
    assert m.profit_factor == pytest.approx(6.0)
    assert m.avg_trade_return == pytest.approx(0.25 / 3)


def test_no_trades_gives_nan_trade_stats() -> None:
    base = result()
    empty = BacktestResult(
        initial_cash=base.initial_cash,
        equity=base.equity,
        fills=base.fills.clear(),
        trades=base.trades.clear(),
        positions={},
    )
    m = compute_metrics(empty)
    assert m.trades == 0
    assert math.isnan(m.win_rate)
    assert math.isnan(m.profit_factor)


def test_report_mentions_key_metrics() -> None:
    text = format_metrics(compute_metrics(result()))
    for label in ("CAGR", "Sharpe", "Max drawdown", "Profit factor"):
        assert label in text
