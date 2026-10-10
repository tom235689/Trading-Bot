from dataclasses import replace
from datetime import UTC, date, datetime
from itertools import pairwise
from pathlib import Path

import polars as pl
import pytest

from factories import price_bars
from tbot.backtest.config import BacktestConfig
from tbot.backtest.engine import BacktestResult
from tbot.cli import main
from tbot.core.timeframe import Timeframe
from tbot.data.store import BarStore
from tbot.research.report import format_report
from tbot.research.sweep import (
    SweepRun,
    best_run,
    grid_points,
    neighborhood_mean,
    rank_of,
    run_sweep,
    select_run,
    sweep_table,
)
from tbot.research.trials import TrialLog
from tbot.research.validate import WalkForwardConfig, load_validation_config, run_validation
from tbot.research.walkforward import Window, add_months, run_walk_forward, stitch, windows

H4 = Timeframe.H4
T0 = datetime(2024, 1, 1, tzinfo=UTC)
GRID = {"entry": [5, 10], "exit": [3, 5]}

BASE = BacktestConfig.model_validate(
    {
        "start": "2024-01-01",
        "end": "2024-07-01",
        "initial_cash": 1000,
        "strategies": [
            {
                "name": "donchian_trend",
                "symbols": ["BTCUSDT"],
                "timeframe": "4h",
                "allocation": 1.0,
                "params": {"entry": 5, "exit": 3},
            }
        ],
    }
)

VALIDATION = """
backtest: base.yaml
holdout_start: 2024-05-01
grid: {entry: [5, 10], exit: [3, 5]}
min_trades: 3
walk_forward: {train_months: 2, test_months: 1}
monte_carlo: {runs: 200, seed: 1}
gate: {min_oos_sharpe: 0.5, max_drawdown: 0.3, min_trades: 5}
"""


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Six months of 4h bars cycling up 2% for 30 bars, then down 2% for 30 bars."""
    closes = [100.0]
    for i in range(1, 1100):
        closes.append(closes[-1] * (1.02 if (i // 30) % 2 == 0 else 0.98))
    root = tmp_path / "data"
    BarStore(root).write("BTCUSDT", H4, price_bars(T0, H4, [closes[0], *closes[:-1]], closes))
    return root


def test_grid_points() -> None:
    assert grid_points(GRID) == [
        {"entry": 5, "exit": 3},
        {"entry": 5, "exit": 5},
        {"entry": 10, "exit": 3},
        {"entry": 10, "exit": 5},
    ]


def test_sweep_ranks_and_neighborhood(data_dir: Path) -> None:
    runs = run_sweep(BASE, GRID, data_dir)
    assert [run.params for run in runs] == grid_points(GRID)
    assert all(run.metrics.trades > 0 for run in runs)

    best = best_run(runs, "sharpe", min_trades=1)
    assert rank_of(runs, best.params, "sharpe") == 1
    assert best.metrics.sharpe == max(run.metrics.sharpe for run in runs)
    with pytest.raises(ValueError, match="no parameter set"):
        best_run(runs, "sharpe", min_trades=10**6)  # nothing qualifies

    everything = neighborhood_mean(runs, GRID, best.params, "sharpe", 1)
    assert everything == pytest.approx(sum(r.metrics.sharpe for r in runs) / len(runs))


def test_sweep_in_processes(data_dir: Path) -> None:
    serial = run_sweep(BASE, {"entry": [5, 10]}, data_dir, workers=1)
    parallel = run_sweep(BASE, {"entry": [5, 10]}, data_dir, workers=2)
    assert [r.metrics for r in parallel] == [r.metrics for r in serial]
    # The grid varies entry only; exit keeps the base value instead of the default.
    assert [r.config.strategies[0].params for r in serial] == [
        {"entry": 5, "exit": 3},
        {"entry": 10, "exit": 3},
    ]


def test_no_trade_runs_rank_last(data_dir: Path) -> None:
    runs = run_sweep(BASE, {"entry": [5, 10_000], "exit": [3]}, data_dir)
    assert runs[1].metrics.trades == 0
    assert sweep_table(runs, "sharpe")["entry"].to_list() == [5, 10_000]
    assert rank_of(runs, {"entry": 10_000, "exit": 3}, "sharpe") == 2
    assert best_run(runs, "sharpe", min_trades=1) is runs[0]


def test_neighborhood_selection_prefers_a_plateau(data_dir: Path) -> None:
    grid = {"entry": [5, 10, 15, 20, 25]}
    runs = run_sweep(BASE, grid, data_dir)
    scores = [2.0, 0.5, 1.4, 1.5, 1.2]  # a lone peak at the edge, a plateau around 20

    def scored(trades: dict[int, int]) -> list[SweepRun]:
        return [
            replace(run, metrics=replace(run.metrics, sharpe=score, trades=trades.get(i, 10)))
            for i, (run, score) in enumerate(zip(runs, scores, strict=True))
        ]

    assert select_run(scored({}), grid, "sharpe", 1, "best").params == {"entry": 5}
    # Neighborhood means, beyond the edges the worst one tested: 1.00, 1.30, 1.13, 1.37, 1.30.
    assert select_run(scored({}), grid, "sharpe", 1, "neighborhood").params == {"entry": 20}
    # A point that does not qualify itself is never picked, whatever its neighbors do.
    assert select_run(scored({3: 0}), grid, "sharpe", 1, "neighborhood").params != {"entry": 20}
    with pytest.raises(ValueError, match="no parameter set"):
        select_run(scored({}), grid, "sharpe", 10**6, "neighborhood")


def test_walk_forward_step_must_match_test_length() -> None:
    assert WalkForwardConfig(train_months=24, test_months=6, step_months=6).step_months == 6
    with pytest.raises(ValueError, match="step_months"):
        WalkForwardConfig(train_months=24, test_months=12, step_months=6)


def test_month_arithmetic_and_windows() -> None:
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert add_months(date(2024, 11, 1), 14) == date(2026, 1, 1)
    plan = windows(date(2018, 1, 1), date(2025, 1, 1), 36, 12)
    assert plan[0] == Window(date(2018, 1, 1), date(2021, 1, 1), date(2022, 1, 1))
    assert plan[-1].test_end == date(2025, 1, 1)
    assert len(plan) == 4
    assert windows(date(2024, 1, 1), date(2024, 6, 1), 36, 12) == []


def test_stitch_chains_segments() -> None:
    def segment(equities: list[float], pnl: float, first: int) -> BacktestResult:
        times = [datetime(2024, 1, first + i, tzinfo=UTC) for i in range(len(equities))]
        return BacktestResult(
            initial_cash=100.0,
            equity=pl.DataFrame({"time": times, "equity": equities, "cash": [0.0] * len(equities)}),
            fills=pl.DataFrame({"quantity": [1.0], "fee": [0.1]}),
            trades=pl.DataFrame({"pnl": [pnl], "fees": [0.1], "cost": [50.0]}),
            positions={"BTCUSDT": 1.0},
        )

    first, second = segment([110.0, 120.0], 20.0, 1), segment([100.0, 90.0], -10.0, 2)
    stitched = stitch([first, second], 100.0)
    # The second segment starts on Jan 2 with 100, the first one's 120 then: one record.
    assert stitched.equity["equity"].to_list() == pytest.approx([110, 120, 108])
    assert stitched.equity["time"].is_unique().all()
    assert stitched.fills["quantity"].to_list() == pytest.approx([1.0, 1.2])
    assert stitched.trades["pnl"].to_list() == pytest.approx([20.0, -12.0])


def test_walk_forward_uses_train_choice_on_test(data_dir: Path) -> None:
    base = BASE.with_period(date(2024, 1, 1), date(2024, 5, 1))
    result = run_walk_forward(
        base, GRID, data_dir, train_months=2, test_months=1, objective="sharpe", min_trades=1
    )
    assert [w.window.train_end for w in result.windows] == [date(2024, 3, 1), date(2024, 4, 1)]
    for window in result.windows:
        assert window.params == best_run(window.train_runs, "sharpe", 1).params
        assert window.test_result.equity["time"][0] >= datetime.combine(
            window.window.train_end, datetime.min.time(), tzinfo=UTC
        )
    heights = sum(w.test_result.equity.height for w in result.windows)
    assert result.stitched.equity.height == heights - 1  # one boundary, recorded once


def write_validation(tmp_path: Path) -> Path:
    (tmp_path / "base.yaml").write_text(
        "start: 2024-01-01\nend: 2024-07-01\ninitial_cash: 1000\n"
        "strategies:\n  - {name: donchian_trend, symbols: [BTCUSDT], timeframe: 4h, "
        "allocation: 1.0, params: {entry: 5, exit: 3}}\n",
        encoding="utf-8",
    )
    path = tmp_path / "validation.yaml"
    path.write_text(VALIDATION, encoding="utf-8")
    return path


def test_validation_end_to_end(tmp_path: Path, data_dir: Path) -> None:
    config, base = load_validation_config(write_validation(tmp_path))
    log = TrialLog(tmp_path / "trials.jsonl")
    stages: list[str] = []

    report = run_validation(config, base, data_dir, log, progress=stages.append)

    assert len(stages) == 5
    assert len(report.sweep_runs) == 4
    assert report.plateau.rank >= 1
    assert len(report.walk_forward.windows) == 2
    assert report.holdout_evaluations == 1
    assert report.baseline.start < report.holdout.start
    assert len(report.gate) == 5
    tags = [r.tag for r in log.read()]
    assert tags.count("baseline") == 1
    assert tags.count("sweep") == 4
    assert tags.count("walkforward-train") == 8
    assert tags.count("holdout") == 1
    # Distinct param sets on the in-sample period with a defined Sharpe: the baseline
    # params are one of the four grid points, so at most four.
    assert 1 < report.deflated.trials <= 4

    text = format_report(report)
    for heading in ("3. Parameter sweep", "4. Walk-forward", "6. Deflated Sharpe", "8. Gate"):
        assert heading in text
    assert ("PASSED" in text) is report.passed
    # Both resampled: the in-sample run and what the strategy did on unseen data.
    assert report.monte_carlo_oos.days < report.monte_carlo.days
    assert "in-sample (" in text
    assert "out-of-sample (" in text
    assert "beyond the 45% kill switch" in text


def test_validation_rejects_bad_holdout(tmp_path: Path) -> None:
    path = write_validation(tmp_path)
    path.write_text(VALIDATION.replace("2024-05-01", "2023-01-01"), encoding="utf-8")
    with pytest.raises(ValueError, match="holdout_start must be after"):
        load_validation_config(path)


def test_validate_command(
    tmp_path: Path, data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_validation(tmp_path)
    code = main(["validate", str(path), "--data-dir", str(data_dir), "--workers", "1"])
    out = capsys.readouterr().out
    assert code in (0, 1)
    assert "8. Gate" in out
    assert (data_dir / "trials.jsonl").exists()


def test_windows_do_not_drift_after_short_months() -> None:
    plan = windows(date(2018, 1, 31), date(2018, 7, 1), 1, 1)
    tests = [(w.train_end, w.test_end) for w in plan]
    assert tests[:3] == [
        (date(2018, 2, 28), date(2018, 3, 31)),
        (date(2018, 3, 31), date(2018, 4, 30)),
        (date(2018, 4, 30), date(2018, 5, 31)),
    ]
    assert all(a[1] == b[0] for a, b in pairwise(tests))


def test_validation_rejects_a_guard(tmp_path: Path) -> None:
    path = write_validation(tmp_path)
    base = tmp_path / "base.yaml"
    base.write_text(
        base.read_text(encoding="utf-8") + "guard: {max_drawdown: 0.3}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="without a guard"):
        load_validation_config(path)


def test_a_lone_peak_among_failing_neighbors_is_no_plateau(data_dir: Path) -> None:
    grid = {"entry": [5, 10, 15, 20, 25]}
    runs = run_sweep(BASE, grid, data_dir)
    scores = [1.3, 1.4, 1.3, -1.0, 2.0]  # 20 trades too little to qualify
    trades = [40, 40, 40, 5, 40]
    scored = [
        replace(run, metrics=replace(run.metrics, sharpe=score, trades=count))
        for run, score, count in zip(runs, scores, trades, strict=True)
    ]
    assert select_run(scored, grid, "sharpe", 30, "neighborhood").params != {"entry": 25}
    assert neighborhood_mean(scored, grid, {"entry": 25}, "sharpe", 30) == pytest.approx(0.0)


def test_a_lone_peak_in_a_corner_is_no_plateau(data_dir: Path) -> None:
    grid = {"entry": [5, 10, 15, 20], "exit": [3, 5, 7, 9]}
    runs = run_sweep(BASE, grid, data_dir)

    def score(params: dict[str, int]) -> float:
        if params == {"entry": 5, "exit": 3}:
            return 2.3  # the corner
        plateau = params["entry"] >= 10 and params["exit"] >= 5
        return 1.3 if plateau else 1.0

    scored = [replace(r, metrics=replace(r.metrics, sharpe=score(r.params))) for r in runs]
    # Four tested cells would give the corner (2.3 + 1.0 + 1.0 + 1.3) / 4 = 1.4; beyond the
    # edge counts as its worst tested neighbor, so it scores as the same peak inside would.
    corner = neighborhood_mean(scored, grid, {"entry": 5, "exit": 3}, "sharpe", 1)
    assert corner == pytest.approx((2.3 + 1.0 + 1.0 + 1.3 + 5 * 1.0) / 9)
    pick = select_run(scored, grid, "sharpe", 1, "neighborhood")
    assert pick.params == {"entry": 15, "exit": 7}
