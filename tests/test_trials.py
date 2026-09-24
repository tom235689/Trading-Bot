import math
from datetime import UTC, datetime
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
    assert log.selection_sharpes(like) == [1.0]  # holdout and nan excluded
    assert log.count("donchian_trend", "holdout") == 1
    assert log.selection_sharpes(like.model_copy(update={"symbols": ["ETHUSDT"]})) == []

    log.append([make_record(CONFIG, metrics(2.0), "sweep")])  # same params again
    assert log.selection_sharpes(like) == [2.0]  # latest run replaces, count unchanged
