"""Bar-by-bar trading session: the backtest's decision path, one event at a time."""

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

import polars as pl

from tbot.core.config import TradingConfig
from tbot.core.timeframe import Timeframe, from_millis
from tbot.live.history import BarHistory
from tbot.live.ledger import EquityPoint
from tbot.portfolio.allocation import StrategySlot, decide_orders, validate_targets
from tbot.portfolio.portfolio import Portfolio
from tbot.strategies.base import StrategyContext

StreamKey = tuple[str, Timeframe]


class TradingSession:
    """Holds strategies, bar histories, marks, and the portfolio.

    `ingest` mirrors one backtest event: append the bars that just closed, mark
    positions, run the strategies whose bars closed, and plan orders. Execution
    is the caller's job, so the same session serves paper and live brokers.
    """

    def __init__(
        self,
        config: TradingConfig,
        slots: Sequence[StrategySlot],
        histories: Mapping[StreamKey, BarHistory],
        portfolio: Portfolio,
    ) -> None:
        self.config = config
        self.slots = list(slots)
        self.histories = dict(histories)
        self.portfolio = portfolio
        for slot in self.slots:
            for symbol in slot.strategy.symbols:
                if (symbol, slot.timeframe) not in self.histories:
                    raise ValueError(f"no history for {symbol} {slot.timeframe}")
        # Finest timeframe per symbol drives marks.
        self.exec_keys: dict[str, StreamKey] = {}
        for symbol, timeframe in sorted(self.histories, key=lambda key: key[1].millis):
            self.exec_keys.setdefault(symbol, (symbol, timeframe))
        self.marks: dict[str, float] = {
            symbol: self.histories[key].last_close
            for symbol, key in self.exec_keys.items()
            if len(self.histories[key])
        }

    @property
    def keys(self) -> list[StreamKey]:
        return list(self.histories)

    def ingest(self, closed: Mapping[StreamKey, pl.DataFrame], now: datetime) -> dict[str, float]:
        """Process bars that closed at `now`; return signed order quantities per symbol."""
        self._append(closed)
        if not self._run_strategies(closed.keys(), now):
            return {}
        cfg = self.config
        return decide_orders(self.slots, self.portfolio, self.marks, cfg.risk, cfg.rebalance)

    def replay(self, closed: Mapping[StreamKey, pl.DataFrame], now: datetime) -> None:
        """Warm up: same as ingest but nothing is traded."""
        self._append(closed)
        self._run_strategies(closed.keys(), now)

    def snapshot(self, now: datetime) -> EquityPoint:
        equity = self.portfolio.equity(self.marks)
        gross = sum(abs(q) * self.marks[s] for s, q in self.portfolio.positions.items())
        exposure = gross / equity if equity > 0 else 0.0
        return EquityPoint(now, equity, self.portfolio.cash, exposure)

    def _append(self, closed: Mapping[StreamKey, pl.DataFrame]) -> None:
        for key, bars in closed.items():
            history = self.histories[key]
            history.append(bars)
            if self.exec_keys[key[0]] == key:
                self.marks[key[0]] = history.last_close

    def _run_strategies(self, revealed: Iterable[StreamKey], now: datetime) -> bool:
        revealed = set(revealed)
        ran = False
        for slot in self.slots:
            symbols = slot.strategy.symbols
            if not any((s, slot.timeframe) in revealed for s in symbols):
                continue
            ctx = StrategyContext(
                time=now,
                windows={s: self.histories[(s, slot.timeframe)].window() for s in symbols},
                exposures=self.portfolio.exposures(self.marks),
            )
            slot.targets = validate_targets(slot.strategy, slot.strategy.on_bar(ctx))
            ran = True
        return ran


def replay_history(session: TradingSession, frames: Mapping[StreamKey, pl.DataFrame]) -> None:
    """Feed stored bars through the session in close-time order, as the backtest would."""
    events: dict[int, dict[StreamKey, pl.DataFrame]] = {}
    for key, frame in frames.items():
        for row in frame.sort("open_time").iter_slices(1):
            close_ms = int(row["open_time"].dt.epoch("ms")[0]) + key[1].millis
            events.setdefault(close_ms, {})[key] = row
    for close_ms in sorted(events):
        session.replay(events[close_ms], from_millis(close_ms))
