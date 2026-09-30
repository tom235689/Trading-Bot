"""Order execution: simulated for paper, Binance spot for testnet and live."""

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Protocol
from uuid import uuid4

import httpx
import structlog

from tbot.core.models import Fill
from tbot.core.timeframe import to_millis
from tbot.exchange.binance import (
    NOTHING_TO_CANCEL,
    BinanceError,
    BinanceSpot,
    Order,
    aggregate_fill,
)
from tbot.execution.sim_broker import SimulatedBroker
from tbot.live.ledger import Ledger
from tbot.monitoring.telegram import Notifier
from tbot.portfolio.portfolio import Portfolio

log = structlog.get_logger(__name__)

STOP_PREFIX = "tbs"
BUY_MARGIN = 0.002  # keep this much quote free for price movement between quote and fill
STOP_LIMIT_GAP = 0.005  # limit price below the stop price so a triggered stop fills


class PriceSource(Protocol):
    async def price(self, symbol: str, side: int) -> float:
        """Executable price now: ask for a buy (side > 0), bid for a sell."""


class Executor(Protocol):
    async def execute(
        self,
        orders: Mapping[str, float],
        now: datetime,
        portfolio: Portfolio,
        marks: Mapping[str, float],
    ) -> list[Fill]: ...

    async def after_event(self, portfolio: Portfolio, marks: Mapping[str, float]) -> None:
        """Housekeeping once an event's orders are done (exchange-side stops)."""


def order_id(now: datetime, symbol: str, quantity: float) -> str:
    """Deterministic per bar event, symbol, and side, so a retry cannot double an order."""
    return f"tb{to_millis(now)}{symbol}{'B' if quantity > 0 else 'S'}"[:36]


def _sorted(orders: Mapping[str, float]) -> list[tuple[str, float]]:
    return sorted(orders.items(), key=lambda item: item[1])  # sells first fund buys


class PaperExecutor:
    """Fills at once at the current book price through the simulated broker."""

    def __init__(
        self,
        broker: SimulatedBroker,
        prices: PriceSource,
        ledger: Ledger,
        notifier: Notifier,
        clock: Callable[[], datetime],
    ) -> None:
        self.broker = broker
        self.prices = prices
        self.ledger = ledger
        self.notifier = notifier
        self.clock = clock

    async def execute(
        self,
        orders: Mapping[str, float],
        now: datetime,
        portfolio: Portfolio,
        marks: Mapping[str, float],
    ) -> list[Fill]:
        fills = []
        for symbol, quantity in _sorted(orders):
            try:
                reference = await self.prices.price(symbol, 1 if quantity > 0 else -1)
            except Exception as exc:  # a failed quote skips this order only
                log.error("price_failed", symbol=symbol, error=repr(exc))
                self.ledger.add_order(now, symbol, quantity, "failed", repr(exc))
                await self.notifier.send(f"[paper] price lookup failed for {symbol}: {exc!r}")
                continue
            fill = self.broker.fill(
                self.clock(),
                symbol,
                quantity,
                reference,
                portfolio.cash,
                portfolio.position(symbol),
            )
            if fill is None:
                self.ledger.add_order(now, symbol, quantity, "skipped", "no cash or position")
                continue
            portfolio.apply(fill)
            self.ledger.add_fill(fill, reference)
            self.ledger.add_order(now, symbol, quantity, "filled")
            await self.notifier.send(_fill_text("paper", fill, portfolio.equity(marks)))
            fills.append(fill)
        return fills

    async def after_event(self, portfolio: Portfolio, marks: Mapping[str, float]) -> None:
        return None


class LiveExecutor:
    """Market orders on Binance spot with write-ahead, idempotent placement, and stops."""

    def __init__(
        self,
        spot: BinanceSpot,
        ledger: Ledger,
        notifier: Notifier,
        clock: Callable[[], datetime],
        *,
        fee_rate: float,
        protective_stop_pct: float,
        label: str = "live",
    ) -> None:
        self.spot = spot
        self.ledger = ledger
        self.notifier = notifier
        self.clock = clock
        self.fee_rate = fee_rate
        self.protective_stop_pct = protective_stop_pct
        self.label = label

    async def execute(
        self,
        orders: Mapping[str, float],
        now: datetime,
        portfolio: Portfolio,
        marks: Mapping[str, float],
    ) -> list[Fill]:
        fills = []
        for symbol, quantity in _sorted(orders):
            try:
                fill = await self._execute_one(symbol, quantity, now, portfolio)
            except (BinanceError, httpx.HTTPError) as exc:
                log.error("order_failed", symbol=symbol, quantity=quantity, error=repr(exc))
                self.ledger.add_order(now, symbol, quantity, "failed", repr(exc))
                await self.notifier.send(f"[{self.label}] order failed for {symbol}: {exc}")
                continue
            if fill is not None:
                fills.append(fill)
                await self.notifier.send(_fill_text(self.label, fill, portfolio.equity(marks)))
        return fills

    async def _execute_one(
        self, symbol: str, quantity: float, now: datetime, portfolio: Portfolio
    ) -> Fill | None:
        rules = self.spot.rules[symbol]
        client_id = order_id(now, symbol, quantity)
        order = await self.spot.get_order(symbol, client_id)
        if order is not None:
            log.warning("order_recovered", symbol=symbol, client_order_id=client_id)
            reference = order.quote_qty / order.executed_qty if order.executed_qty else 0.0
        else:
            if quantity < 0:
                await self.cancel_stops(symbol)  # stops lock the base balance
            balances = await self.spot.balances()
            reference = await self.spot.book_price(symbol, 1 if quantity > 0 else -1)
            if quantity > 0:
                free = balances[rules.quote].free if rules.quote in balances else 0.0
                quantity = min(quantity, free / (reference * (1 + self.fee_rate + BUY_MARGIN)))
            else:
                free = balances[rules.base].free if rules.base in balances else 0.0
                quantity = max(quantity, -free)
            quantity = rules.round_quantity(quantity)
            if quantity == 0 or not rules.acceptable(quantity, reference):
                self.ledger.add_order(now, symbol, quantity, "skipped", "below exchange minimum")
                return None
            self.ledger.add_order(now, symbol, quantity, "pending", client_id)  # write-ahead
            order = await self._place(symbol, quantity, client_id)
            if order is None:
                self.ledger.add_order(now, symbol, quantity, "failed", "no order on exchange")
                return None
        if order.executed_qty == 0:
            self.ledger.add_order(now, symbol, quantity, "unfilled", order.status)
            return None
        fill = await self._to_fill(order, rules.base, rules.quote)
        portfolio.apply(fill)
        self.ledger.add_fill(fill, reference)
        self.ledger.add_order(now, symbol, quantity, "filled", client_id)
        return fill

    async def _place(self, symbol: str, quantity: float, client_id: str) -> Order | None:
        """Send the order; on a transport failure, ask the exchange whether it went through."""
        try:
            return await self.spot.market_order(symbol, quantity, client_id)
        except httpx.HTTPError as exc:
            log.warning("order_send_uncertain", symbol=symbol, error=repr(exc))
            return await self.spot.get_order(symbol, client_id)

    async def _to_fill(self, order: Order, base: str, quote: str) -> Fill:
        """Commission in the base asset shrinks the quantity; every commission becomes quote fee."""
        fill, commissions = aggregate_fill(order, self.clock())
        assert fill is not None
        quantity, fee = fill.quantity, 0.0
        for asset, amount in commissions.items():
            if asset == quote:
                fee += amount
            elif asset == base:
                fee += amount * fill.price
                if quantity > 0:
                    quantity -= amount
            else:
                try:
                    fee += amount * await self.spot.last_price(f"{asset}{quote}")
                except (BinanceError, httpx.HTTPError) as exc:
                    log.warning("fee_unpriced", asset=asset, amount=amount, error=repr(exc))
        return replace(fill, quantity=quantity, fee=fee)

    # exchange-side protective stops

    async def cancel_stops(self, symbol: str) -> int:
        """Cancel this bot's stop orders; one that just triggered is already gone."""
        count = 0
        for order in await self.spot.open_orders(symbol):
            if not order.client_order_id.startswith(STOP_PREFIX):
                continue
            try:
                await self.spot.cancel_order(symbol, order.order_id)
            except BinanceError as exc:
                if exc.code != NOTHING_TO_CANCEL:
                    raise
                log.info("stop_already_gone", symbol=symbol, order_id=order.order_id)
                continue
            count += 1
        return count

    async def after_event(self, portfolio: Portfolio, marks: Mapping[str, float]) -> None:
        if not self.protective_stop_pct:
            return
        for symbol, rules in self.spot.rules.items():
            try:
                await self.cancel_stops(symbol)
                position = portfolio.position(symbol)
                if position <= 0 or symbol not in marks:
                    continue
                balances = await self.spot.balances()
                free = balances[rules.base].free if rules.base in balances else 0.0
                quantity = rules.round_quantity(min(position, free))
                stop = rules.round_price(marks[symbol] * (1 - self.protective_stop_pct))
                limit = rules.round_price(stop * (1 - STOP_LIMIT_GAP))
                if quantity == 0 or not rules.acceptable(quantity, limit):
                    continue
                client_id = f"{STOP_PREFIX}{symbol}{uuid4().hex[:12]}"
                await self.spot.stop_loss_order(symbol, quantity, stop, limit, client_id)
                log.info("stop_placed", symbol=symbol, quantity=quantity, stop=stop)
            except (BinanceError, httpx.HTTPError) as exc:
                log.error("stop_failed", symbol=symbol, error=repr(exc))
                await self.notifier.send(
                    f"[{self.label}] protective stop failed for {symbol}: {exc}"
                )


class BinanceBookTicker:
    """Paper price source: the public book ticker."""

    URL = "https://data-api.binance.vision/api/v3/ticker/bookTicker"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def price(self, symbol: str, side: int) -> float:
        response = await self.client.get(self.URL, params={"symbol": symbol}, timeout=10.0)
        response.raise_for_status()
        data = response.json()
        return float(data["askPrice"] if side > 0 else data["bidPrice"])


def _fill_text(label: str, fill: Fill, equity: float) -> str:
    side = "BUY" if fill.quantity > 0 else "SELL"
    return (
        f"[{label}] {side} {abs(fill.quantity):.6f} {fill.symbol} @ {fill.price:,.2f} "
        f"fee {fill.fee:.2f} | equity {equity:,.2f}"
    )
