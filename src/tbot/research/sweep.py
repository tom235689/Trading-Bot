"""One backtest per parameter combination."""

import math
import sys
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from itertools import product
from pathlib import Path
from typing import Any

import polars as pl

from tbot.backtest.config import BacktestConfig
from tbot.backtest.engine import BacktestResult
from tbot.backtest.metrics import Metrics, compute_metrics
from tbot.backtest.runner import run_backtest
from tbot.data.store import BarStore

Grid = Mapping[str, Sequence[Any]]

OBJECTIVES = ("sharpe", "sortino", "cagr", "calmar", "total_return")
SELECTIONS = ("best", "neighborhood")  # how walk-forward picks params on a train window
TABLE_METRICS = ("total_return", "cagr", "sharpe", "sortino", "max_drawdown", "trades", "fees")


@dataclass(frozen=True)
class SweepRun:
    params: dict[str, Any]
    config: BacktestConfig
    metrics: Metrics


def grid_points(grid: Grid) -> list[dict[str, Any]]:
    names = list(grid)
    combos = product(*(grid[name] for name in names))
    return [dict(zip(names, values, strict=True)) for values in combos]


def evaluate(config: BacktestConfig, data_dir: Path) -> tuple[BacktestResult, Metrics]:
    result = run_backtest(config, BarStore(data_dir))
    return result, compute_metrics(result)


def _evaluate_job(job: tuple[str, str]) -> dict[str, Any]:
    """Worker entry point; config travels as JSON so it pickles across processes."""
    config_json, data_dir = job
    return asdict(evaluate(BacktestConfig.model_validate_json(config_json), Path(data_dir))[1])


def run_sweep(
    base: BacktestConfig, grid: Grid, data_dir: Path, *, workers: int = 1
) -> list[SweepRun]:
    points = grid_points(grid)
    configs = [base.with_params(point) for point in points]
    jobs = [(config.model_dump_json(), str(data_dir)) for config in configs]
    if sys.platform == "win32":
        workers = min(workers, 61)  # the most a process pool takes on Windows
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            raw = list(pool.map(_evaluate_job, jobs))
    else:
        raw = [_evaluate_job(job) for job in jobs]
    return [
        SweepRun(point, config, Metrics(**metrics))
        for point, config, metrics in zip(points, configs, raw, strict=True)
    ]


def objective_value(metrics: Metrics, objective: str, min_trades: int) -> float:
    """Objective to maximize; runs with too few trades do not qualify."""
    value: float = getattr(metrics, objective)
    if metrics.trades < min_trades or not math.isfinite(value):
        return -math.inf
    return value


def best_run(runs: Sequence[SweepRun], objective: str, min_trades: int) -> SweepRun:
    """The qualifying run with the highest objective; an error when none qualifies."""
    scores = [objective_value(run.metrics, objective, min_trades) for run in runs]
    index = max(range(len(runs)), key=lambda i: scores[i])
    if scores[index] == -math.inf:
        raise ValueError(f"no parameter set has {min_trades} trades and a finite {objective}")
    return runs[index]


def select_run(
    runs: Sequence[SweepRun], grid: Grid, objective: str, min_trades: int, selection: str
) -> SweepRun:
    """The run to trade next: the best qualifying objective, or for `neighborhood` the
    qualifying run whose grid neighbors do best on average (a plateau, not a lone peak)."""
    if selection == "best":
        return best_run(runs, objective, min_trades)
    if selection != "neighborhood":
        raise ValueError(f"selection must be one of {SELECTIONS}")
    qualifying = [
        run for run in runs if math.isfinite(objective_value(run.metrics, objective, min_trades))
    ]
    if not qualifying:
        raise ValueError(f"no parameter set has {min_trades} trades and a finite {objective}")
    return max(
        qualifying,
        key=lambda run: neighborhood_mean(runs, grid, run.params, objective, min_trades),
    )


def sweep_table(runs: Sequence[SweepRun], objective: str) -> pl.DataFrame:
    rows = [
        {**run.params, **{name: getattr(run.metrics, name) for name in TABLE_METRICS}}
        for run in runs
    ]
    # NaN sorts above every number in polars; treat it as missing so it lands at the bottom.
    return pl.DataFrame(rows).sort(
        pl.col(objective).fill_nan(None), descending=True, nulls_last=True
    )


def neighborhood_mean(
    runs: Sequence[SweepRun],
    grid: Grid,
    center: Mapping[str, Any],
    objective: str,
    min_trades: int,
) -> float:
    """Mean objective over grid points within one step of center in every parameter.

    A robust parameter set sits on a plateau: its neighbors do nearly as well.
    """
    index = {name: {value: i for i, value in enumerate(values)} for name, values in grid.items()}

    def near(params: Mapping[str, Any]) -> bool:
        return all(abs(index[n][params[n]] - index[n][center[n]]) <= 1 for n in grid)

    scores = [objective_value(run.metrics, objective, min_trades) for run in runs]
    # A neighbor with too few trades counts, not as missing, but as no better than its own
    # result, break-even, or the worst qualifying run: else a lone peak among failing
    # neighbors would look like a plateau.
    floor = min((score for score in scores if math.isfinite(score)), default=0.0)
    values = []
    for run, score in zip(runs, scores, strict=True):
        if not near(run.params):
            continue
        if not math.isfinite(score):
            raw = float(getattr(run.metrics, objective))
            score = min(raw if math.isfinite(raw) else 0.0, 0.0, floor)
        values.append(score)
    return sum(values) / len(values) if values else math.nan


def rank_of(runs: Sequence[SweepRun], params: Mapping[str, Any], objective: str) -> int:
    """1-based rank of params by raw objective among all runs."""
    scores = [float(getattr(run.metrics, objective)) for run in runs]
    target = next(s for s, run in zip(scores, runs, strict=True) if run.params == dict(params))
    if not math.isfinite(target):
        return len(runs)
    return 1 + sum(score > target for score in scores)
