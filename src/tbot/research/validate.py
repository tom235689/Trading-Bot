"""Run the validation pipeline for one strategy."""

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from tbot.backtest.config import BacktestConfig, load_config
from tbot.backtest.metrics import DAYS_PER_YEAR, Metrics, compute_metrics, daily_returns
from tbot.research.montecarlo import MonteCarloSummary, simulate, trade_returns_on_equity
from tbot.research.statistics import daily_sharpe, deflated_sharpe, expected_max_sharpe, moments
from tbot.research.sweep import (
    OBJECTIVES,
    Grid,
    SweepRun,
    best_run,
    evaluate,
    neighborhood_mean,
    rank_of,
    run_sweep,
)
from tbot.research.trials import TrialLog, make_record
from tbot.research.walkforward import WalkForwardResult, run_walk_forward


class WalkForwardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    train_months: int = Field(default=36, ge=1)
    test_months: int = Field(default=12, ge=1)
    step_months: int | None = Field(default=None, ge=1)


class MonteCarloConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    runs: int = Field(default=2000, ge=100)
    seed: int = 1
    replace: bool = False  # True bootstraps trades with replacement instead of shuffling


class GateConfig(BaseModel):
    """Promotion thresholds. Out-of-sample means the stitched walk-forward test segments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    min_oos_sharpe: float = 0.8
    max_drawdown: float = Field(default=0.25, gt=0, lt=1)
    min_trades: int = Field(default=100, ge=1)


class ValidationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backtest: Path  # base backtest config, relative to this file
    holdout_start: date  # data from here on is only used by the final holdout run
    grid: dict[str, list[Any]] = Field(min_length=1)
    objective: str = "sharpe"
    min_trades: int = Field(default=30, ge=1)  # runs with fewer trades cannot be selected
    walk_forward: WalkForwardConfig = WalkForwardConfig()
    monte_carlo: MonteCarloConfig = MonteCarloConfig()
    cost_multiplier: float = Field(default=2.0, ge=1)
    gate: GateConfig = GateConfig()

    @field_validator("objective")
    @classmethod
    def check_objective(cls, value: str) -> str:
        if value not in OBJECTIVES:
            raise ValueError(f"objective must be one of {OBJECTIVES}")
        return value


def load_validation_config(path: Path) -> tuple[ValidationConfig, BacktestConfig]:
    config = ValidationConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    base = load_config(path.parent / config.backtest)
    if len(base.strategies) != 1:
        raise ValueError("validation targets exactly one strategy")
    if config.holdout_start <= base.start:
        raise ValueError("holdout_start must be after the backtest start")
    if base.end is not None and config.holdout_start >= base.end:
        raise ValueError("holdout_start must be before the backtest end")
    return config, base


@dataclass(frozen=True)
class GateCheck:
    name: str
    value: float
    threshold: float
    passed: bool


@dataclass(frozen=True)
class DeflatedSharpe:
    trials: int
    probability: float  # that the true Sharpe is above zero, given the trials
    expected_max_sharpe: float  # annualized Sharpe pure luck would reach over the trials


@dataclass(frozen=True)
class Plateau:
    """How the base params sit in the sweep landscape."""

    rank: int  # 1-based by objective, 0 if the params are outside the grid
    neighborhood: float  # mean objective of the params and their grid neighbors
    best_neighborhood: float  # same for the best grid point


@dataclass(frozen=True)
class ValidationReport:
    config: ValidationConfig
    base: BacktestConfig
    baseline: Metrics
    stress: Metrics
    sweep_runs: list[SweepRun]
    best: SweepRun
    plateau: Plateau
    walk_forward: WalkForwardResult
    oos: Metrics
    monte_carlo: MonteCarloSummary
    deflated: DeflatedSharpe
    holdout: Metrics
    holdout_evaluations: int
    gate: list[GateCheck]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.gate)


def run_validation(
    config: ValidationConfig,
    base: BacktestConfig,
    data_dir: Path,
    log: TrialLog,
    *,
    workers: int = 1,
    progress: Callable[[str], None] = lambda _: None,
) -> ValidationReport:
    strategy = base.strategies[0]
    in_sample = base.with_period(base.start, config.holdout_start)
    objective, min_trades = config.objective, config.min_trades

    progress("baseline and cost stress")
    baseline_result, baseline = evaluate(in_sample, data_dir)
    baseline_record = make_record(in_sample, baseline, "baseline")
    _, stress = evaluate(in_sample.with_cost_multiplier(config.cost_multiplier), data_dir)
    log.append([baseline_record, make_record(in_sample, stress, "stress")])

    progress(f"parameter sweep over {math.prod(len(v) for v in config.grid.values())} points")
    runs = run_sweep(in_sample, config.grid, data_dir, workers=workers)
    log.append([make_record(run.config, run.metrics, "sweep") for run in runs])
    best = best_run(runs, objective, min_trades)

    wf = config.walk_forward
    progress(f"walk-forward (train {wf.train_months}m, test {wf.test_months}m)")
    walk = run_walk_forward(
        in_sample,
        config.grid,
        data_dir,
        train_months=wf.train_months,
        test_months=wf.test_months,
        step_months=wf.step_months,
        objective=objective,
        min_trades=min_trades,
        workers=workers,
    )
    for window in walk.windows:
        records = [make_record(r.config, r.metrics, "walkforward-train") for r in window.train_runs]
        log.append(records)
    oos = compute_metrics(walk.stitched)

    progress("Monte Carlo and deflated Sharpe")
    mc = config.monte_carlo
    monte_carlo = simulate(
        trade_returns_on_equity(baseline_result),
        runs=mc.runs,
        seed=mc.seed,
        replace=mc.replace,
        drawdown_limit=config.gate.max_drawdown,
    )
    deflated = _deflated(daily_returns(baseline_result), log.selection_sharpes(baseline_record))

    progress("holdout")
    _, holdout = evaluate(base.with_period(config.holdout_start, base.end), data_dir)
    log.append([make_record(base, holdout, "holdout")])

    return ValidationReport(
        config=config,
        base=base,
        baseline=baseline,
        stress=stress,
        sweep_runs=runs,
        best=best,
        plateau=_plateau(runs, config.grid, strategy.params, best.params, objective, min_trades),
        walk_forward=walk,
        oos=oos,
        monte_carlo=monte_carlo,
        deflated=deflated,
        holdout=holdout,
        holdout_evaluations=log.count(strategy.name, "holdout"),
        gate=_gate(config, oos, stress, holdout),
    )


def _plateau(
    runs: list[SweepRun],
    grid: Grid,
    params: dict[str, Any],
    best_params: dict[str, Any],
    objective: str,
    min_trades: int,
) -> Plateau:
    in_grid = set(params) == set(grid) and all(params[n] in grid[n] for n in grid)
    return Plateau(
        rank=rank_of(runs, params, objective) if in_grid else 0,
        neighborhood=(
            neighborhood_mean(runs, grid, params, objective, min_trades) if in_grid else math.nan
        ),
        best_neighborhood=neighborhood_mean(runs, grid, best_params, objective, min_trades),
    )


def _deflated(returns: npt.NDArray[np.float64], annual_sharpes: list[float]) -> DeflatedSharpe:
    trials = [daily_sharpe(s) for s in annual_sharpes]
    variance = float(np.var(trials, ddof=1)) if len(trials) > 1 else 0.0
    std = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    sharpe = float(returns.mean()) / std if std > 0 else math.nan
    skew, kurt = moments(returns)
    probability = deflated_sharpe(sharpe, len(returns), skew, kurt, variance, len(trials))
    luck = expected_max_sharpe(variance, len(trials)) * math.sqrt(DAYS_PER_YEAR)
    return DeflatedSharpe(len(trials), probability, luck)


def _gate(
    config: ValidationConfig, oos: Metrics, stress: Metrics, holdout: Metrics
) -> list[GateCheck]:
    gate = config.gate
    dd_limit = -gate.max_drawdown
    return [
        GateCheck(
            "out-of-sample Sharpe",
            oos.sharpe,
            gate.min_oos_sharpe,
            oos.sharpe >= gate.min_oos_sharpe,
        ),
        GateCheck(
            "out-of-sample max drawdown", oos.max_drawdown, dd_limit, oos.max_drawdown >= dd_limit
        ),
        GateCheck(
            "out-of-sample trades", oos.trades, gate.min_trades, oos.trades >= gate.min_trades
        ),
        GateCheck(
            f"return at {config.cost_multiplier:g}x costs",
            stress.total_return,
            0,
            stress.total_return > 0,
        ),
        GateCheck("holdout return", holdout.total_return, 0, holdout.total_return > 0),
    ]
