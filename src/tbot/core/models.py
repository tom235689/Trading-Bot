"""Records shared by portfolio, execution, and backtest."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Fill:
    time: datetime
    symbol: str
    quantity: float  # signed: buy > 0, sell < 0
    price: float
    fee: float  # quote currency

    @property
    def notional(self) -> float:
        return abs(self.quantity) * self.price


@dataclass(frozen=True)
class Trade:
    """Round trip of one symbol from flat back to flat."""

    symbol: str
    direction: int  # 1 long, -1 short
    entry_time: datetime
    exit_time: datetime
    pnl: float  # after fees
    fees: float
    cost: float  # notional of fills that opened or added to the position

    @property
    def return_on_cost(self) -> float:
        return self.pnl / self.cost
