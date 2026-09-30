"""Cash, positions, and round-trip trade tracking."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from tbot.core.models import Fill, Trade

# Positions smaller than this are float residue and count as flat.
EPSILON = 1e-12


@dataclass
class _OpenTrade:
    direction: int
    entry_time: datetime
    cash_flow: float = 0.0
    fees: float = 0.0
    cost: float = 0.0


class Portfolio:
    def __init__(self, cash: float, dust_notional: float = 0.0) -> None:
        self.cash = cash
        # A remainder worth less than this after a sale cannot be traded: the round trip ends.
        self.dust_notional = dust_notional
        self.positions: dict[str, float] = {}
        self.fills: list[Fill] = []
        self.trades: list[Trade] = []
        self._open: dict[str, _OpenTrade] = {}

    def position(self, symbol: str) -> float:
        return self.positions.get(symbol, 0.0)

    def equity(self, prices: Mapping[str, float]) -> float:
        return self.cash + sum(qty * prices[symbol] for symbol, qty in self.positions.items())

    def exposures(self, prices: Mapping[str, float]) -> dict[str, float]:
        """Position value per symbol as a fraction of equity."""
        equity = self.equity(prices)
        if equity <= 0:
            return {}
        return {symbol: qty * prices[symbol] / equity for symbol, qty in self.positions.items()}

    def adjust(self, symbol: str, quantity: float, cash: float) -> None:
        """Reconciliation delta from the exchange; bypasses trade tracking.

        A position the exchange closed ends its round trip without a record, so the
        next fill in that symbol starts a fresh one instead of extending a stale trade.
        """
        self.cash += cash
        if symbol:
            after = self.position(symbol) + quantity
            if abs(after) < EPSILON:
                self.positions.pop(symbol, None)
                self._open.pop(symbol, None)
            else:
                self.positions[symbol] = after

    def apply(self, fill: Fill) -> None:
        self.fills.append(fill)
        self.cash -= fill.quantity * fill.price + fill.fee
        before = self.position(fill.symbol)
        after = before + fill.quantity
        if abs(after) < EPSILON:
            after = 0.0
        dust = (
            after != 0
            and (after > 0) == (before > 0)
            and abs(after) < abs(before)
            and abs(after) * fill.price < self.dust_notional
        )

        if before and (after == 0 or (after > 0) != (before > 0)):
            # Closes the position, maybe flipping it: split the fill at zero.
            share = -before / fill.quantity
            self._track(fill, -before, fill.fee * share, _sign(before))
            self._close(fill.symbol, fill.time)
            if after:
                self._track(fill, after, fill.fee * (1 - share), _sign(after))
        elif dust and fill.symbol in self._open:
            # Sold down to an untradable remainder: the round trip is over, the dust stays.
            self._track(fill, fill.quantity, fill.fee, _sign(before))
            self._close(fill.symbol, fill.time)
        else:
            self._track(fill, fill.quantity, fill.fee, _sign(after))

        if after:
            self.positions[fill.symbol] = after
        else:
            self.positions.pop(fill.symbol, None)

    def _track(self, fill: Fill, quantity: float, fee: float, direction: int) -> None:
        """Add a fill to the open round trip; `direction` is the position's side if it opens one.

        A position adopted from the exchange has no open trade, so its first sale opens
        one on the position's side rather than looking like a short.
        """
        trade = self._open.get(fill.symbol)
        if trade is None:
            trade = _OpenTrade(direction=direction, entry_time=fill.time)
            self._open[fill.symbol] = trade
        trade.cash_flow -= quantity * fill.price + fee
        trade.fees += fee
        if (quantity > 0) == (trade.direction > 0):
            trade.cost += abs(quantity) * fill.price

    def _close(self, symbol: str, time: datetime) -> None:
        trade = self._open.pop(symbol)
        self.trades.append(
            Trade(
                symbol=symbol,
                direction=trade.direction,
                entry_time=trade.entry_time,
                exit_time=time,
                pnl=trade.cash_flow,
                fees=trade.fees,
                cost=trade.cost,
            )
        )


def _sign(value: float) -> int:
    return 1 if value > 0 else -1
