"""Append-only log of every backtest run, so multiple testing can be counted."""

import hashlib
import json
import math
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from tbot.backtest.config import BacktestConfig
from tbot.backtest.metrics import Metrics

# Tags whose runs count as selection attempts on a sample.
SELECTION_TAGS = frozenset({"backtest", "baseline", "sweep", "walkforward-train"})


class TrialRecord(BaseModel):
    # Metrics can be NaN (no trades, no Sharpe); write them as JSON constants, not null.
    model_config = ConfigDict(extra="forbid", frozen=True, ser_json_inf_nan="constants")

    time: datetime
    tag: str
    strategy: str
    symbols: list[str]
    timeframe: str
    params: dict[str, Any]
    start: date
    end: date | None
    fee_rate: float
    slippage_bps: float
    sharpe: float
    sortino: float
    cagr: float
    max_drawdown: float
    total_return: float
    trades: int
    setup: str = ""  # hash of everything else that shapes the run; empty in old logs


def setup_hash(config: BacktestConfig) -> str:
    """Risk, costs, rebalance rules, and other strategies: what makes the same params
    a different trial."""
    data = config.model_dump(mode="json", exclude={"start", "end"})
    data["strategies"][0]["params"] = {}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def make_record(config: BacktestConfig, metrics: Metrics, tag: str) -> TrialRecord:
    strategy = config.strategies[0]
    return TrialRecord(
        time=datetime.now(UTC),
        tag=tag,
        strategy=strategy.name,
        symbols=strategy.symbols,
        timeframe=str(strategy.timeframe),
        params=dict(strategy.params),
        start=config.start,
        end=config.end,
        fee_rate=config.costs.fee_rate,
        slippage_bps=config.costs.slippage_bps,
        sharpe=metrics.sharpe,
        sortino=metrics.sortino,
        cagr=metrics.cagr,
        max_drawdown=metrics.max_drawdown,
        total_return=metrics.total_return,
        trades=metrics.trades,
        setup=setup_hash(config),
    )


class TrialLog:
    """JSON lines file, one record per backtest run."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, records: list[TrialRecord]) -> None:
        if not records:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as file:  # LF on every OS
            for record in records:
                file.write(record.model_dump_json() + "\n")

    def read(self) -> list[TrialRecord]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [TrialRecord.model_validate_json(line) for line in lines if line.strip()]

    def selection_sharpes(self, like: TrialRecord) -> list[float]:
        """Annualized Sharpe of each distinct trial on the same sample; NaN when undefined.

        Same sample means the strategy, symbols, timeframe, and period of `like`. A trial is
        params and their result: re-running the same setup gives the same result and is not
        a new trial, while other risk settings or costs give another result and are. Old logs
        carry no setup, so the result is what tells their trials apart.
        """
        trials: dict[tuple[str, str], float] = {}
        for r in self.read():
            same = (r.strategy, r.symbols, r.timeframe, r.start, r.end) == (
                like.strategy,
                like.symbols,
                like.timeframe,
                like.start,
                like.end,
            )
            if same and r.tag in SELECTION_TAGS:
                params = json.dumps(r.params, sort_keys=True, default=str)
                result = f"{r.sharpe:.6f}" if math.isfinite(r.sharpe) else "nan"
                trials[(params, result)] = r.sharpe
        return list(trials.values())

    def holdout_looks(self, like: TrialRecord, holdout_start: date) -> int:
        """Runs of this strategy, symbols, and timeframe that saw data from holdout_start on,
        whatever their tag: every backtest over that period is a look at the holdout."""
        return sum(
            1
            for r in self.read()
            if (r.strategy, r.symbols, r.timeframe) == (like.strategy, like.symbols, like.timeframe)
            and (r.end is None or r.end > holdout_start)
        )

    def count(self, strategy: str, tag: str) -> int:
        return sum(1 for r in self.read() if r.strategy == strategy and r.tag == tag)
