"""Order execution: simulated for paper, Binance spot for testnet and live."""

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Protocol, TypedDict
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
    SymbolRules,
    TradeFill,
    aggregate_fill,
)
from tbot.execution.sim_broker import SimulatedBroker
from tbot.live.ledger import Ledger, OrderRecord
from tbot.monitoring.telegram import Notifier
from tbot.portfolio.portfolio import Portfolio

log = structlog.get_logger(__name__)

STOP_PREFIX = "tbs"
STOP_META = "stop:"  # + symbol: the protective stop in force and how much of it is booked
BUY_MARGIN = 0.002  # keep this much quote free for price movement between quote and fill
STOP_LIMIT_GAP = 0.005  # limit price below the stop price so a triggered stop fills
LOOKUP_DELAYS = (0.5, 1.0, 2.0, 4.0)  # seconds between lookups of an order in doubt


class OrderUnknown(Exception):
    """An order was sent, but the exchange could not say whether it executed."""


class StopState(TypedDict):
    id: str
    qty: float  # executed quantity already booked
    quote: float  # executed quote amount already booked


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


def order_id(now: datetime, symbol: str, quantity: float, attempt: int = 1) -> str:
    """Client order id from the bar event, symbol, and side; a repeat decision adds `r<n>`."""
    suffix = "" if attempt == 1 else f"r{attempt}"
    head = f"tb{to_millis(now)}{symbol}"[: 35 - len(suffix)]
    return f"{head}{'B' if quantity > 0 else 'S'}{suffix}"


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
                self.ledger.add_order(
                    now, symbol, quantity, "skipped", "no cash, position, or price"
                )
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
    """Market orders on Binance spot with write-ahead rows, recovery, and protective stops.

    Every order gets a client id written to the ledger before it is sent. An order
    whose outcome is unclear (lost response, 5xx) is looked up by that id; one that
    stays unclear is resolved by `settle`, which runs before every reconciliation.
    """

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
            client_id = self._new_id(now, symbol, quantity)
            try:
                fill = await self._execute_one(symbol, quantity, now, portfolio, client_id)
            except OrderUnknown as exc:
                log.error("order_unknown", symbol=symbol, client_order_id=client_id)
                self.ledger.add_order(now, symbol, quantity, "unknown", str(exc), client_id)
                await self.notifier.send(
                    f"[{self.label}] order state unknown for {symbol}; it is looked up again "
                    f"before the next reconciliation"
                )
                continue
            except (BinanceError, httpx.HTTPError) as exc:
                log.error("order_failed", symbol=symbol, quantity=quantity, error=repr(exc))
                self.ledger.add_order(now, symbol, quantity, "failed", repr(exc), client_id)
                await self.notifier.send(f"[{self.label}] order failed for {symbol}: {exc}")
                continue
            if fill is not None:
                fills.append(fill)
                await self.notifier.send(_fill_text(self.label, fill, portfolio.equity(marks)))
        return fills

    def _new_id(self, now: datetime, symbol: str, quantity: float) -> str:
        """A second decision at the same close (a late stream) must not reuse an id."""
        attempt = 1
        while self.ledger.client_id_used(client_id := order_id(now, symbol, quantity, attempt)):
            attempt += 1
        return client_id

    async def _execute_one(
        self, symbol: str, quantity: float, now: datetime, portfolio: Portfolio, client_id: str
    ) -> Fill | None:
        rules = self.spot.rules[symbol]
        if quantity < 0:
            await self.cancel_stops(symbol, portfolio)  # stops lock the base balance
        balances = await self.spot.balances()
        reference = await self.spot.book_price(symbol, 1 if quantity > 0 else -1)
        if not reference > 0:
            self.ledger.add_order(now, symbol, quantity, "skipped", "no price in the book")
            return None
        if quantity > 0:
            # Spend neither more than the account holds nor more than the book's own cash:
            # with budget ownership the rest of the account is the owner's money.
            free = balances[rules.quote].free if rules.quote in balances else 0.0
            spendable = min(free, portfolio.cash)
            quantity = min(quantity, spendable / (reference * (1 + self.fee_rate + BUY_MARGIN)))
        else:
            free = balances[rules.base].free if rules.base in balances else 0.0
            quantity = max(quantity, -free)
        quantity = rules.round_quantity(quantity)
        if quantity == 0 or not rules.acceptable(quantity, reference):
            self.ledger.add_order(now, symbol, quantity, "skipped", "below exchange minimum")
            return None
        self.ledger.add_order(now, symbol, quantity, "pending", "", client_id)  # write-ahead
        order = await self._place(symbol, quantity, client_id)
        if order is None:
            self.ledger.add_order(now, symbol, quantity, "failed", "not on exchange", client_id)
            return None
        return await self._book(order, rules, now, quantity, reference, portfolio, client_id)

    async def _place(self, symbol: str, quantity: float, client_id: str) -> Order | None:
        """Send the order. When the answer leaves its fate open, ask the exchange by client id.

        Returns None only when the exchange says it never saw the order; raises
        OrderUnknown when it cannot be asked.
        """
        try:
            return await self.spot.market_order(symbol, quantity, client_id)
        except BinanceError as exc:
            if not exc.uncertain:
                raise  # rejected: nothing executed
            cause: Exception = exc
        except httpx.HTTPError as exc:
            cause = exc
        log.warning("order_send_uncertain", symbol=symbol, error=repr(cause))
        answered = False
        for delay in LOOKUP_DELAYS:
            await asyncio.sleep(delay)
            try:
                order = await self.spot.get_order(symbol, client_id)
            except (BinanceError, httpx.HTTPError) as exc:
                log.warning("order_lookup_failed", symbol=symbol, error=repr(exc))
                continue
            if order is not None:
                log.warning("order_recovered", symbol=symbol, client_order_id=client_id)
                return order
            answered = True
        if answered:
            return None
        raise OrderUnknown(f"{symbol} {client_id}: sent, then {cause!r}; lookups failed")

    async def _book(
        self,
        order: Order,
        rules: SymbolRules,
        time: datetime,
        requested: float,
        reference: float,
        portfolio: Portfolio,
        client_id: str,
    ) -> Fill | None:
        if order.executed_qty == 0:
            self.ledger.add_order(
                time, order.symbol, requested, "unfilled", order.status, client_id
            )
            return None
        fill = await self._to_fill(order, rules)
        portfolio.apply(fill)
        self.ledger.add_fill(fill, reference)
        self.ledger.add_order(time, order.symbol, fill.quantity, "filled", "", client_id)
        return fill

    async def _to_fill(self, order: Order, rules: SymbolRules) -> Fill:
        """Commission in the base asset shrinks the quantity; every commission becomes quote fee.

        An order read back by lookup carries no executions, so they are fetched.
        """
        trades: list[TradeFill] | None = None
        if not order.fills:
            try:
                trades = await self.spot.my_trades(order.symbol, order.order_id)
            except (BinanceError, httpx.HTTPError) as exc:
                log.warning("trades_unavailable", symbol=order.symbol, error=repr(exc))
                trades = [
                    TradeFill(0.0, 0.0, order.quote_qty * self.fee_rate, rules.quote)
                ]  # estimate at the configured rate
        fill, commissions = aggregate_fill(order, self.clock(), trades)
        assert fill is not None
        quantity, fee = fill.quantity, 0.0
        for asset, amount in commissions.items():
            if asset == rules.quote:
                fee += amount
            elif asset == rules.base:
                fee += amount * fill.price
                if quantity > 0:
                    quantity -= amount
            else:
                try:
                    fee += amount * await self.spot.last_price(f"{asset}{rules.quote}")
                except (BinanceError, httpx.HTTPError) as exc:
                    log.warning("fee_unpriced", asset=asset, amount=amount, error=repr(exc))
        return replace(fill, quantity=quantity, fee=fee)

    # outside events: orders in doubt and stops that executed

    async def settle(self, portfolio: Portfolio) -> None:
        """Book what happened on the exchange between events. Run before reconciling."""
        for record in self.ledger.unresolved_orders():
            if record.symbol not in self.spot.rules:  # no longer traded: the operator decides
                log.warning("unresolved_order_skipped", symbol=record.symbol)
                continue
            await self._resolve(record, portfolio)
        for symbol in self.spot.rules:
            await self._settle_stop(symbol, portfolio)

    async def _resolve(self, record: OrderRecord, portfolio: Portfolio) -> None:
        order = await self.spot.get_order(record.symbol, record.client_id)
        if order is None:
            self.ledger.add_order(
                record.time,
                record.symbol,
                record.quantity,
                "failed",
                "not on exchange",
                record.client_id,
            )
            return
        if not order.done:
            return  # still working; asked again next time
        rules = self.spot.rules[record.symbol]
        price = order.quote_qty / order.executed_qty if order.executed_qty else 0.0
        fill = await self._book(
            order, rules, record.time, record.quantity, price, portfolio, record.client_id
        )
        log.warning("order_resolved", symbol=record.symbol, client_order_id=record.client_id)
        if fill is not None:
            await self.notifier.send(
                f"[{self.label}] booked an order whose result was not recorded: "
                f"{_fill_summary(fill)}"
            )

    async def _settle_stop(self, symbol: str, portfolio: Portfolio) -> None:
        """Book the executed part of this bot's protective stop; forget it once it is done."""
        state = self._stop_state(symbol)
        if state is None:
            return
        order = await self.spot.get_order(symbol, state["id"])
        if order is None:  # never reached the exchange
            self._set_stop_state(symbol, None)
            return
        quantity = order.executed_qty - state["qty"]
        if quantity > 1e-12:
            quote = order.quote_qty - state["quote"]
            price = quote / quantity
            fee = quote * self.fee_rate  # a stop's commission is not in its order record
            fill = Fill(self.clock(), symbol, -quantity, price, fee)
            portfolio.apply(fill)
            self.ledger.add_fill(fill, order.stop_price or price)
            self.ledger.add_order(
                fill.time, symbol, fill.quantity, "filled", "protective stop", state["id"]
            )
            log.warning("stop_executed", symbol=symbol, quantity=quantity, price=price)
            await self.notifier.send(
                f"[{self.label}] protective stop sold {quantity:.6f} {symbol} @ {price:,.2f}"
            )
            state = StopState(id=state["id"], qty=order.executed_qty, quote=order.quote_qty)
        self._set_stop_state(symbol, None if order.done else state)

    def _stop_state(self, symbol: str) -> StopState | None:
        raw = self.ledger.get_meta(STOP_META + symbol)
        if raw is None:
            return None
        data = json.loads(raw)
        return StopState(id=str(data["id"]), qty=float(data["qty"]), quote=float(data["quote"]))

    def _set_stop_state(self, symbol: str, state: StopState | None) -> None:
        if state is None:
            self.ledger.delete_meta(STOP_META + symbol)
        else:
            self.ledger.set_meta(STOP_META + symbol, json.dumps(state))

    # exchange-side protective stops

    async def cancel_stops(self, symbol: str, portfolio: Portfolio) -> int:
        """Cancel this bot's stop orders, then book whatever of them executed first."""
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
        await self._settle_stop(symbol, portfolio)
        return count

    async def after_event(self, portfolio: Portfolio, marks: Mapping[str, float]) -> None:
        if not self.protective_stop_pct:
            return
        for symbol, rules in self.spot.rules.items():
            try:
                await self.cancel_stops(symbol, portfolio)
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
                self._set_stop_state(symbol, StopState(id=client_id, qty=0.0, quote=0.0))
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


def _fill_summary(fill: Fill) -> str:
    side = "BUY" if fill.quantity > 0 else "SELL"
    return f"{side} {abs(fill.quantity):.6f} {fill.symbol} @ {fill.price:,.2f} fee {fill.fee:.2f}"


def _fill_text(label: str, fill: Fill, equity: float) -> str:
    return f"[{label}] {_fill_summary(fill)} | equity {equity:,.2f}"
