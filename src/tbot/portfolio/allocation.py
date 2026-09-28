"""From strategy targets to orders. Shared by backtest, paper, and live."""

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import tbot.strategies  # noqa: F401  # registers built-in strategies
from tbot.core.config import StrategyConfig
from tbot.core.timeframe import Timeframe
from tbot.execution.rebalance import RebalanceRules, plan_orders
from tbot.portfolio.portfolio import Portfolio
from tbot.risk.limits import RiskLimits
from tbot.strategies.base import Strategy
from tbot.strategies.registry import create_strategy


@dataclass
class StrategySlot:
    strategy: Strategy
    timeframe: Timeframe
    allocation: float  # fraction of portfolio equity
    targets: dict[str, float] = field(default_factory=dict)  # latest validated output


def build_slots(configs: Sequence[StrategyConfig]) -> list[StrategySlot]:
    return [
        StrategySlot(create_strategy(c.name, c.symbols, c.params), c.timeframe, c.allocation)
        for c in configs
    ]


def validate_targets(strategy: Strategy, targets: Mapping[str, float]) -> dict[str, float]:
    for symbol, weight in targets.items():
        if symbol not in strategy.symbols:
            raise ValueError(f"{strategy.name}: target for unsubscribed symbol {symbol}")
        if not (math.isfinite(weight) and -1 <= weight <= 1):
            raise ValueError(f"{strategy.name}: target {weight} for {symbol} is outside [-1, 1]")
    return dict(targets)


def combine_targets(slots: Sequence[StrategySlot]) -> dict[str, float]:
    """Allocation-weighted sum of strategy targets; opposite views on a symbol net out."""
    weights: dict[str, float] = defaultdict(float)
    for slot in slots:
        for symbol, weight in slot.targets.items():
            weights[symbol] += slot.allocation * weight
    return dict(weights)


def decide_orders(
    slots: Sequence[StrategySlot],
    portfolio: Portfolio,
    marks: Mapping[str, float],
    risk: RiskLimits,
    rules: RebalanceRules,
) -> dict[str, float]:
    """Signed order quantities that move the portfolio to the risk-limited combined targets."""
    equity = portfolio.equity(marks)
    if equity <= 0:
        return {}
    weights = risk.apply(combine_targets(slots))
    return plan_orders(weights, portfolio.positions, marks, equity, rules)
