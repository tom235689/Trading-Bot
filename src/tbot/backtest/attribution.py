"""Per-strategy attribution: each strategy alone, the combination, and their correlation."""

from dataclasses import dataclass

import numpy as np

from tbot.backtest.config import BacktestConfig
from tbot.backtest.metrics import Metrics, compute_metrics, daily_returns
from tbot.backtest.runner import run_backtest
from tbot.data.store import BarStore


@dataclass(frozen=True)
class AttributionRow:
    label: str
    allocation: float
    metrics: Metrics


@dataclass(frozen=True)
class Attribution:
    rows: list[AttributionRow]  # one per strategy alone, then the combination
    correlation: list[list[float]]  # daily return correlation between the solo runs


def run_attribution(config: BacktestConfig, store: BarStore) -> Attribution:
    """Run every strategy on its own with full capital, then the configured combination."""
    rows = []
    solo_returns = []
    for strategy in config.strategies:
        solo = config.model_copy(
            update={"strategies": [strategy.model_copy(update={"allocation": 1.0})]}
        )
        result = run_backtest(solo, store)
        rows.append(AttributionRow(strategy.name, strategy.allocation, compute_metrics(result)))
        solo_returns.append(daily_returns(result))
    combined = run_backtest(config, store)
    rows.append(AttributionRow("combined", 1.0, compute_metrics(combined)))

    length = min(len(r) for r in solo_returns)
    matrix = (
        np.corrcoef(np.vstack([r[:length] for r in solo_returns]))
        if len(rows) > 2
        else np.ones((1, 1))
    )
    return Attribution(rows, np.atleast_2d(matrix).tolist())


def format_attribution(attribution: Attribution) -> str:
    lines = [f"{'strategy':<18}{'alloc':>6}{'CAGR':>8}{'Sharpe':>8}{'MaxDD':>8}{'Trades':>8}"]
    for row in attribution.rows:
        m = row.metrics
        lines.append(
            f"{row.label:<18}{row.allocation:>6.0%}{m.cagr:>8.1%}{m.sharpe:>8.2f}"
            f"{m.max_drawdown:>8.1%}{m.trades:>8d}"
        )
    names = [row.label for row in attribution.rows[:-1]]
    if len(names) > 1:
        lines.append("daily return correlation (strategies alone):")
        for name, values in zip(names, attribution.correlation, strict=True):
            lines.append(f"  {name:<16}" + "".join(f"{v:>7.2f}" for v in values))
    return "\n".join(lines)
