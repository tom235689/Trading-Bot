"""Build and run a backtest from config and stored bars."""

from datetime import UTC, date, datetime, time

import polars as pl

import tbot.strategies  # noqa: F401  # registers built-in strategies
from tbot.backtest.config import BacktestConfig
from tbot.backtest.engine import BacktestEngine, BacktestResult, StrategySlot, StreamKey
from tbot.data.store import BarStore
from tbot.strategies.registry import create_strategy

# Extra history loaded before start, as a multiple of warmup, to cover data gaps.
WARMUP_MARGIN = 2


def _utc(day: date) -> datetime:
    return datetime.combine(day, time(), tzinfo=UTC)


def run_backtest(config: BacktestConfig, store: BarStore) -> BacktestResult:
    start = _utc(config.start)
    end = _utc(config.end) if config.end else None
    slots = [
        StrategySlot(create_strategy(sc.name, sc.symbols, sc.params), sc.timeframe, sc.allocation)
        for sc in config.strategies
    ]

    # Each stream loads enough history before start for its longest warmup.
    lookback: dict[StreamKey, int] = {}
    for slot in slots:
        for symbol in slot.strategy.symbols:
            key = (symbol, slot.timeframe)
            lookback[key] = max(lookback.get(key, 0), slot.strategy.warmup)
    bars: dict[StreamKey, pl.DataFrame] = {}
    for (symbol, timeframe), warmup in lookback.items():
        frame = store.read(symbol, timeframe, start - timeframe.delta * warmup * WARMUP_MARGIN, end)
        if frame.is_empty():
            raise ValueError(f"no stored bars for {symbol} {timeframe}; run `tbot download`")
        bars[(symbol, timeframe)] = frame

    engine = BacktestEngine(
        slots,
        bars,
        start=start,
        initial_cash=config.initial_cash,
        costs=config.costs,
        risk=config.risk,
        rules=config.rebalance,
    )
    return engine.run()
