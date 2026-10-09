import math
from datetime import UTC, date, datetime
from pathlib import Path

from tbot.backtest.config import BacktestConfig
from tbot.backtest.metrics import Metrics
from tbot.research.trials import TrialLog, make_record


def metrics(sharpe: float) -> Metrics:
    now = datetime(2024, 1, 1, tzinfo=UTC)
    nan = math.nan
    return Metrics(
        now, now, 1.0, 1.0, 1.0, 0.0, 0.0, sharpe, nan, 0.0, nan, 0.0, 0.0, 0.0, 0, nan, nan, nan
    )


CONFIG = BacktestConfig.model_validate(
    {
        "start": "2024-01-01",
        "strategies": [
            {
                "name": "donchian_trend",
                "symbols": ["BTCUSDT"],
                "timeframe": "4h",
                "allocation": 1.0,
                "params": {"entry": 5},
            }
        ],
    }
)


def test_log_round_trip_and_filters(tmp_path: Path) -> None:
    log = TrialLog(tmp_path / "trials.jsonl")
    assert log.read() == []

    log.append([make_record(CONFIG, metrics(1.0), "sweep")])
    log.append(
        [
            make_record(CONFIG, metrics(0.5), "holdout"),
            make_record(CONFIG, metrics(math.nan), "sweep"),
        ]
    )

    records = log.read()
    assert [r.tag for r in records] == ["sweep", "holdout", "sweep"]
    assert records[0].params == {"entry": 5}
    assert records[0].timeframe == "4h"
    like = records[0]
    sharpes = log.selection_sharpes(like)  # the holdout is no selection; a NaN run is a trial
    assert sharpes[0] == 1.0
    assert len(sharpes) == 2
    assert math.isnan(sharpes[1])
    assert log.count("donchian_trend", "holdout") == 1
    assert log.selection_sharpes(like.model_copy(update={"symbols": ["ETHUSDT"]})) == []

    log.append([make_record(CONFIG, metrics(1.0), "sweep")])  # the same run again
    assert len(log.selection_sharpes(like)) == 2
    tighter = CONFIG.model_copy(
        update={"risk": CONFIG.risk.model_copy(update={"target_volatility": 0.4})}
    )
    log.append([make_record(tighter, metrics(1.2), "sweep")])  # same params, other risk
    assert len(log.selection_sharpes(like)) == 3


def test_every_run_over_the_holdout_is_a_look(tmp_path: Path) -> None:
    log = TrialLog(tmp_path / "trials.jsonl")
    in_sample = CONFIG.with_period(date(2024, 1, 1), date(2025, 1, 1))
    log.append(
        [
            make_record(in_sample, metrics(1.0), "sweep"),
            make_record(CONFIG, metrics(1.0), "backtest"),  # to the latest bar
            make_record(CONFIG.with_period(date(2025, 1, 1), None), metrics(0.4), "holdout"),
        ]
    )
    like = log.read()[-1]
    assert log.holdout_looks(like, date(2025, 1, 1)) == 2


def test_log_lines_end_in_lf(tmp_path: Path) -> None:
    log = TrialLog(tmp_path / "trials.jsonl")
    log.append([make_record(CONFIG, metrics(1.0), "sweep")])
    assert b"\r\n" not in log.path.read_bytes()


def test_a_line_cut_by_a_crash_is_skipped_and_kept_apart(tmp_path: Path) -> None:
    log = TrialLog(tmp_path / "trials.jsonl")
    log.append([make_record(CONFIG, metrics(1.0), "sweep")])
    with log.path.open("ab") as file:
        file.write(b'{"time": "2026-10-09T08')  # the process died while appending
    log.append([make_record(CONFIG, metrics(0.5), "sweep")])
    assert [record.sharpe for record in log.read()] == [1.0, 0.5]
