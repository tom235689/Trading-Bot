"""Simulated spot broker with fees and slippage."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from tbot.core.models import Fill


class CostModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fee_rate: float = Field(default=0.001, ge=0, lt=1)  # Binance spot taker, VIP 0
    slippage_bps: float = Field(default=5.0, ge=0)  # adverse move from the reference price


class SimulatedBroker:
    """Fills market orders at a reference price. Spot rules: no borrowing, no shorting."""

    def __init__(self, costs: CostModel) -> None:
        self.costs = costs

    def fill(
        self,
        time: datetime,
        symbol: str,
        quantity: float,
        reference_price: float,
        cash: float,
        position: float,
    ) -> Fill | None:
        slip = self.costs.slippage_bps / 10_000
        price = reference_price * (1 + slip if quantity > 0 else 1 - slip)
        if quantity > 0:
            quantity = min(quantity, cash / (price * (1 + self.costs.fee_rate)))
            if quantity <= 0:
                return None
        else:
            quantity = max(quantity, -position)
            if quantity >= 0:
                return None
        return Fill(time, symbol, quantity, price, abs(quantity) * price * self.costs.fee_rate)
