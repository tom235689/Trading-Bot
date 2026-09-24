"""Turn target weights into orders."""

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

    A zero target closes the exact position. Other changes smaller than the
    rebalance threshold are skipped to avoid paying fees for drift.
    """
    orders = {}
    for symbol in sorted(set(targets) | set(positions)):
        if symbol not in prices:
            continue
        target = targets.get(symbol, 0.0)
        current = positions.get(symbol, 0.0)
        delta = -current if target == 0 else target * equity / prices[symbol] - current
        notional = abs(delta) * prices[symbol]
        if notional == 0 or notional < rules.min_notional:
            continue
        if target != 0 and notional < rules.rebalance_threshold * equity:
            continue
        orders[symbol] = delta
    return orders
