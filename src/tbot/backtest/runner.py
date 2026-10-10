"""Build and run a backtest from config and stored bars."""

from collections.abc import Sequence
from datetime import UTC, date, datetime, time

import polars as pl

from tbot.backtest.config import BacktestConfig
from tbot.backtest.engine import BacktestEngine, BacktestResult, StreamKey
from tbot.core.config import TradingConfig
from tbot.data.store import BarStore
from tbot.portfolio.allocation import StrategySlot, build_slots
from tbot.risk.guard import GuardConfig
from tbot.risk.limits import RiskLimits
from tbot.risk.volatility import lookback_bars as volatility_bars

# Extra history loaded before start, as a multiple of the bars needed, to cover data gaps.
WARMUP_MARGIN = 2


def _utc(day: date) -> datetime:
    return datetime.combine(day, time(), tzinfo=UTC)


def history_bars(slots: Sequence[StrategySlot], risk: RiskLimits) -> dict[StreamKey, int]:
    """Closed bars each stream needs before the first decision.

    Every stream needs the warmup of its strategies. The finest stream of a symbol
    also drives volatility targeting, so it needs the volatility window plus one close.
    """
    lookback: dict[StreamKey, int] = {}
    for slot in slots:
        for symbol in slot.strategy.symbols:
            key = (symbol, slot.timeframe)
            lookback[key] = max(lookback.get(key, 0), slot.strategy.warmup)
    if risk.target_volatility:
        finest: dict[str, StreamKey] = {}
        for key in sorted(lookback, key=lambda key: key[1].millis):
            finest.setdefault(key[0], key)
        for key in finest.values():
            needed = volatility_bars(key[1], risk.volatility_lookback_days) + 1
            lookback[key] = max(lookback[key], needed)
    return lookback


def run_backtest(
    config: BacktestConfig, store: BarStore, *, until: datetime | None = None
) -> BacktestResult:
    """Backtest config's period; until, if earlier than its end, stops the run there."""
    ends = [end for end in (_utc(config.end) if config.end else None, until) if end is not None]
    return run_period(config, store, _utc(config.start), min(ends, default=None), config.guard)


def run_period(
    config: TradingConfig,
    store: BarStore,
    start: datetime,
    end: datetime | None,
    guard: GuardConfig | None = None,
) -> BacktestResult:
    """Trade config's settings from the first bar close at or after start to end, if given.
    Every stream stops at the earliest last close among them, so none is valued at a stale
    price while the others move on (`data_ends` tells which)."""
    slots = build_slots(config.strategies)

    bars: dict[StreamKey, pl.DataFrame] = {}
    for (symbol, timeframe), needed in history_bars(slots, config.risk).items():
        frame = store.read(symbol, timeframe, start - timeframe.delta * needed * WARMUP_MARGIN, end)
        if frame.is_empty():
            raise ValueError(f"no stored bars for {symbol} {timeframe}; run `tbot download`")
        bars[(symbol, timeframe)] = frame
    common = min(_last_close(key, frame) for key, frame in bars.items())
    if end is not None:  # a bar opening before end may close after it
        common = min(common, end)
    bars = {
        key: frame.filter(pl.col("open_time") + key[1].delta <= common)
        for key, frame in bars.items()
    }

    engine = BacktestEngine(
        slots,
        bars,
        start=start,
        initial_cash=config.initial_cash,
        costs=config.costs,
        risk=config.risk,
        rules=config.rebalance,
        guard=guard,
    )
    return engine.run()


def data_ends(config: TradingConfig, store: BarStore) -> dict[StreamKey, datetime]:
    """Close of the last stored bar of every stream the config trades."""
    ends = {}
    for key in history_bars(build_slots(config.strategies), config.risk):
        last = store.last_open_time(*key)
        if last is not None:
            ends[key] = last + key[1].delta
    return ends


def _last_close(key: StreamKey, frame: pl.DataFrame) -> datetime:
    last = frame["open_time"].max()
    assert isinstance(last, datetime)
    return last + key[1].delta
