"""Turn target weights into orders."""

import math
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field


class RebalanceRules(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_notional: float = Field(default=10.0, ge=0)  # Binance spot minimum order value
    rebalance_threshold: float = Field(default=0.02, ge=0, lt=1)  # skip smaller weight changes


def plan_orders(
    targets: Mapping[str, float],
    positions: Mapping[str, float],
    prices: Mapping[str, float],
    equity: float,
    rules: RebalanceRules,
) -> dict[str, float]:
    """Signed quantities that move positions to target weights.

    A zero target closes the exact position, and so does a reduction that would leave
    less than the minimum order value, which could never be sold later. Other changes
    smaller than the rebalance threshold are skipped to avoid paying fees for drift.
    Symbols without a finite, positive price are skipped.
    """
    orders = {}
    for symbol in sorted(set(targets) | set(positions)):
        price = prices.get(symbol)
        if price is None or not (math.isfinite(price) and price > 0):
            continue
        target = targets.get(symbol, 0.0)
        current = positions.get(symbol, 0.0)
        delta = -current if target == 0 else target * equity / price - current
        left = current + delta
        shrinking = current and (left > 0) == (current > 0) and abs(left) < abs(current)
        if target != 0 and shrinking and abs(left) * price < rules.min_notional:
            target, delta = 0.0, -current
        notional = abs(delta) * price
        if notional == 0 or notional < rules.min_notional:
            continue
        if target != 0 and notional < rules.rebalance_threshold * equity:
            continue
        orders[symbol] = delta
    return orders
