"""Event-driven backtest engine.

Each event is a bar close time. At every event, in order:
1. Reveal bars that close now.
2. Fill pending orders at the open of the symbol's newly closed bar (the first
   price after the decision), with slippage and fees.
3. Mark positions at the latest close and record equity.
4. Run strategies whose bars closed now; they see only closed bars.
5. Combine targets, apply risk limits, and queue orders for the next bar.

A symbol's fills and marks use its finest loaded timeframe.
"""

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import numpy.typing as npt
import polars as pl

from tbot.core.timeframe import Timeframe, from_millis, to_millis
from tbot.execution.rebalance import RebalanceRules, plan_orders
from tbot.execution.sim_broker import CostModel, SimulatedBroker
from tbot.portfolio.portfolio import Portfolio
from tbot.risk.limits import RiskLimits
from tbot.strategies.base import BarWindow, Strategy, StrategyContext

StreamKey = tuple[str, Timeframe]


@dataclass
class StrategySlot:
    strategy: Strategy
    timeframe: Timeframe
    allocation: float  # fraction of portfolio equity
    targets: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class PendingOrder:
    decided_ms: int
    symbol: str
    quantity: float


@dataclass(frozen=True)
class BacktestResult:
    initial_cash: float
    equity: pl.DataFrame  # time, equity, cash, exposure
    fills: pl.DataFrame
    trades: pl.DataFrame
    positions: dict[str, float]  # open at the end


def _readonly[T: np.generic](array: npt.NDArray[T]) -> npt.NDArray[T]:
    array = np.ascontiguousarray(array)
    array.flags.writeable = False
    return array


class _Stream:
    """Bars of one symbol and timeframe, revealed one by one as time advances."""

    def __init__(self, bars: pl.DataFrame, timeframe: Timeframe) -> None:
        self.open_ms = _readonly(bars["open_time"].dt.epoch("ms").to_numpy())
        self.close_ms = self.open_ms + timeframe.millis
        self.open_time = _readonly(self.open_ms.astype("datetime64[ms]"))
        self.open = _readonly(bars["open"].to_numpy())
        self.high = _readonly(bars["high"].to_numpy())
        self.low = _readonly(bars["low"].to_numpy())
        self.close = _readonly(bars["close"].to_numpy())
        self.volume = _readonly(bars["volume"].to_numpy())
        self.visible = 0

    def advance(self, now_ms: int) -> bool:
        """Reveal the bar closing at now_ms. Events cover every close time, so one bar at most."""
        if self.visible < len(self.close_ms) and self.close_ms[self.visible] <= now_ms:
            self.visible += 1
            return True
        return False

    def window(self) -> BarWindow:
        n = self.visible
        return BarWindow(
            self.open_time[:n],
            self.open[:n],
            self.high[:n],
            self.low[:n],
            self.close[:n],
            self.volume[:n],
        )


class BacktestEngine:
    def __init__(
        self,
        slots: Sequence[StrategySlot],
        bars: Mapping[StreamKey, pl.DataFrame],
        *,
        start: datetime,
        initial_cash: float,
        costs: CostModel,
        risk: RiskLimits,
        rules: RebalanceRules,
    ) -> None:
        self.slots = list(slots)
        self.streams = {key: _Stream(frame, key[1]) for key, frame in bars.items()}
        for slot in self.slots:
            for symbol in slot.strategy.symbols:
                if (symbol, slot.timeframe) not in self.streams:
                    raise ValueError(f"no bars loaded for {symbol} {slot.timeframe}")
        # Finest timeframe per symbol drives fills and marks.
        self.exec_keys: dict[str, StreamKey] = {}
        for symbol, timeframe in sorted(self.streams, key=lambda key: key[1].millis):
            self.exec_keys.setdefault(symbol, (symbol, timeframe))

        self.start_ms = to_millis(start)
        self.initial_cash = initial_cash
        self.portfolio = Portfolio(initial_cash)
        self.broker = SimulatedBroker(costs)
        self.risk = risk
        self.rules = rules
        self.marks: dict[str, float] = {}
        self.pending: list[PendingOrder] = []
        self.records: list[tuple[datetime, float, float, float]] = []

    def run(self) -> BacktestResult:
        events = np.unique(np.concatenate([s.close_ms for s in self.streams.values()]))
        for now_ms in events.tolist():
            revealed = {key for key, stream in self.streams.items() if stream.advance(now_ms)}
            self._fill_pending(revealed)
            self._mark(revealed)
            trading = now_ms >= self.start_ms
            now = from_millis(now_ms)
            if trading:
                self._record(now)
            if self._run_strategies(now, revealed) and trading:
                self._rebalance(now_ms)
        return self._result()

    def _fill_pending(self, revealed: set[StreamKey]) -> None:
        remaining = []
        # Sells first so their proceeds can fund buys.
        for order in sorted(self.pending, key=lambda o: o.quantity):
            key = self.exec_keys[order.symbol]
            stream = self.streams[key]
            i = stream.visible - 1
            if key not in revealed or stream.open_ms[i] < order.decided_ms:
                remaining.append(order)
                continue
            fill = self.broker.fill(
                from_millis(int(stream.open_ms[i])),
                order.symbol,
                order.quantity,
                float(stream.open[i]),
                self.portfolio.cash,
                self.portfolio.position(order.symbol),
            )
            if fill is not None:
                self.portfolio.apply(fill)
        self.pending = remaining

    def _mark(self, revealed: set[StreamKey]) -> None:
        for symbol, key in self.exec_keys.items():
            if key in revealed:
                stream = self.streams[key]
                self.marks[symbol] = float(stream.close[stream.visible - 1])

    def _record(self, now: datetime) -> None:
        equity = self.portfolio.equity(self.marks)
        gross = sum(abs(q) * self.marks[s] for s, q in self.portfolio.positions.items())
        exposure = gross / equity if equity > 0 else 0.0
        self.records.append((now, equity, self.portfolio.cash, exposure))

    def _run_strategies(self, now: datetime, revealed: set[StreamKey]) -> bool:
        updated = False
        for slot in self.slots:
            symbols = slot.strategy.symbols
            if not any((s, slot.timeframe) in revealed for s in symbols):
                continue
            ctx = StrategyContext(
                time=now,
                windows={s: self.streams[(s, slot.timeframe)].window() for s in symbols},
                exposures=self.portfolio.exposures(self.marks),
            )
            slot.targets = self._validate(slot, slot.strategy.on_bar(ctx))
            updated = True
        return updated

    @staticmethod
    def _validate(slot: StrategySlot, targets: Mapping[str, float]) -> dict[str, float]:
        name = slot.strategy.name
        for symbol, weight in targets.items():
            if symbol not in slot.strategy.symbols:
                raise ValueError(f"{name}: target for unsubscribed symbol {symbol}")
            if not (math.isfinite(weight) and -1 <= weight <= 1):
                raise ValueError(f"{name}: target {weight} for {symbol} is outside [-1, 1]")
        return dict(targets)

    def _rebalance(self, now_ms: int) -> None:
        equity = self.portfolio.equity(self.marks)
        if equity <= 0:
            self.pending = []
            return
        weights: dict[str, float] = defaultdict(float)
        for slot in self.slots:
            for symbol, weight in slot.targets.items():
                weights[symbol] += slot.allocation * weight
        orders = plan_orders(
            self.risk.apply(weights), self.portfolio.positions, self.marks, equity, self.rules
        )
        self.pending = [PendingOrder(now_ms, s, q) for s, q in orders.items()]

    def _result(self) -> BacktestResult:
        time_type = pl.Datetime("ms", "UTC")
        equity = pl.DataFrame(
            self.records,
            schema={
                "time": time_type,
                "equity": pl.Float64,
                "cash": pl.Float64,
                "exposure": pl.Float64,
            },
            orient="row",
        )
        fills = pl.DataFrame(
            [(f.time, f.symbol, f.quantity, f.price, f.fee) for f in self.portfolio.fills],
            schema={
                "time": time_type,
                "symbol": pl.String,
                "quantity": pl.Float64,
                "price": pl.Float64,
                "fee": pl.Float64,
            },
            orient="row",
        )
        trades = pl.DataFrame(
            [
                (
                    t.symbol,
                    t.direction,
                    t.entry_time,
                    t.exit_time,
                    t.pnl,
                    t.fees,
                    t.cost,
                    t.return_on_cost,
                )
                for t in self.portfolio.trades
            ],
            schema={
                "symbol": pl.String,
                "direction": pl.Int64,
                "entry_time": time_type,
                "exit_time": time_type,
                "pnl": pl.Float64,
                "fees": pl.Float64,
                "cost": pl.Float64,
                "return": pl.Float64,
            },
            orient="row",
        )
        return BacktestResult(
            initial_cash=self.initial_cash,
            equity=equity,
            fills=fills,
            trades=trades,
            positions=dict(self.portfolio.positions),
        )
