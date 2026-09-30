"""Walk-forward: pick params on a train window, test on the next, roll on."""

import calendar
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from tbot.backtest.config import BacktestConfig
from tbot.backtest.engine import BacktestResult
from tbot.backtest.metrics import Metrics
from tbot.research.sweep import Grid, SweepRun, best_run, evaluate, objective_value, run_sweep


@dataclass(frozen=True)
class Window:
    train_start: date
    train_end: date  # also the test start
    test_end: date


@dataclass(frozen=True)
class WindowResult:
    window: Window
    params: dict[str, Any]
    train_objective: float
    train_runs: list[SweepRun]
    test_result: BacktestResult
    test_metrics: Metrics


@dataclass(frozen=True)
class WalkForwardResult:
    windows: list[WindowResult]
    stitched: BacktestResult  # test segments chained, each starting from the previous end


def add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year, month = day.year + month_index // 12, month_index % 12 + 1
    return day.replace(
        year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1])
    )


def windows(
    start: date, end: date, train_months: int, test_months: int, step_months: int | None = None
) -> list[Window]:
    """Consecutive windows whose test periods end on or before end."""
    step = step_months or test_months
    result = []
    cursor = start
    while (test_end := add_months(cursor, train_months + test_months)) <= end:
        result.append(Window(cursor, add_months(cursor, train_months), test_end))
        cursor = add_months(cursor, step)
    return result


def stitch(results: Sequence[BacktestResult], initial_cash: float) -> BacktestResult:
    """Chain segments so each starts where the previous ended; quantities scale along."""
    equity, fills, trades = [], [], []
    level = initial_cash
    positions: dict[str, float] = {}
    marks: dict[str, float] = {}
    for result in results:
        if result.equity.is_empty():
            continue
        factor = level / result.initial_cash
        equity.append(
            result.equity.with_columns(pl.col("equity") * factor, pl.col("cash") * factor)
        )
        fills.append(result.fills.with_columns(pl.col("quantity") * factor, pl.col("fee") * factor))
        scaled = [pl.col(name) * factor for name in ("pnl", "fees", "cost")]
        trades.append(result.trades.with_columns(*scaled))
        level = float(result.equity["equity"][-1]) * factor
        positions = {symbol: quantity * factor for symbol, quantity in result.positions.items()}
        marks = result.marks
    if not equity:
        raise ValueError("no walk-forward test segment produced equity records")
    return BacktestResult(
        initial_cash=initial_cash,
        equity=pl.concat(equity),
        fills=pl.concat(fills),
        trades=pl.concat(trades),
        positions=positions,
        marks=marks,
    )


def run_walk_forward(
    base: BacktestConfig,
    grid: Grid,
    data_dir: Path,
    *,
    train_months: int,
    test_months: int,
    step_months: int | None = None,
    objective: str,
    min_trades: int,
    workers: int = 1,
) -> WalkForwardResult:
    if base.end is None:
        raise ValueError("walk-forward needs an explicit end date")
    plan = windows(base.start, base.end, train_months, test_months, step_months)
    if not plan:
        raise ValueError("period is shorter than one train plus test window")
    results = []
    for window in plan:
        train = base.with_period(window.train_start, window.train_end)
        runs = run_sweep(train, grid, data_dir, workers=workers)
        best = best_run(runs, objective, min_trades)
        test = base.with_period(window.train_end, window.test_end).with_params(best.params)
        test_result, test_metrics = evaluate(test, data_dir)
        results.append(
            WindowResult(
                window=window,
                params=best.params,
                train_objective=objective_value(best.metrics, objective, min_trades),
                train_runs=runs,
                test_result=test_result,
                test_metrics=test_metrics,
            )
        )
    stitched = stitch([r.test_result for r in results], base.initial_cash)
    return WalkForwardResult(results, stitched)
