"""Per-strategy attribution: each strategy alone, the combination, and their correlation."""

from dataclasses import dataclass

import numpy as np
import polars as pl

from tbot.backtest.config import BacktestConfig
from tbot.backtest.engine import BacktestResult
from tbot.backtest.metrics import Metrics, compute_metrics
from tbot.backtest.runner import data_ends, run_backtest
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
    # Every row ends where the combination does: at the earliest last close of its streams.
    until = min(data_ends(config, store).values(), default=None)
    rows = []
    solo_returns: list[pl.DataFrame] = []
    for strategy in config.strategies:
        solo = config.model_copy(
            update={"strategies": [strategy.model_copy(update={"allocation": 1.0})]}
        )
        result = run_backtest(solo, store, until=until)
        rows.append(AttributionRow(strategy.name, strategy.allocation, compute_metrics(result)))
        solo_returns.append(_dated_returns(result, f"r{len(solo_returns)}"))
    combined = run_backtest(config, store, until=until)
    rows.append(AttributionRow("combined", 1.0, compute_metrics(combined)))

    matrix = np.ones((1, 1))
    if len(rows) > 2:
        joined = solo_returns[0]
        for frame in solo_returns[1:]:  # only days every run has, paired by date
            joined = joined.join(frame, on="day", how="inner")
        matrix = np.corrcoef(joined.drop("day").to_numpy().T)
    return Attribution(rows, np.atleast_2d(matrix).tolist())


def _dated_returns(result: BacktestResult, name: str) -> pl.DataFrame:
    # A close at 00:00 ends the previous day: shift by 1 ms so a 1d and a 4h run agree.
    closes = result.equity.with_columns(pl.col("time") - pl.duration(milliseconds=1))
    daily = closes.group_by_dynamic("time", every="1d").agg(pl.col("equity").last())
    previous = daily["equity"].shift(1).fill_null(result.initial_cash)
    return pl.DataFrame({"day": daily["time"].dt.date(), name: daily["equity"] / previous - 1})


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
