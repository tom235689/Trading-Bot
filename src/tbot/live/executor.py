"""Order execution: simulated for paper, Binance spot for testnet and live."""

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Protocol, TypedDict
from uuid import uuid4

import httpx
import structlog

from tbot.core.models import Fill
from tbot.core.text import price_text
from tbot.core.timeframe import to_millis
from tbot.exchange.binance import (
    NOTHING_TO_CANCEL,
    RULE_ERRORS,
    TRADING,
    UNLISTED,
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
TAG_META = "order_tag"
BUY_MARGIN = 0.002  # keep this much quote free for price movement between quote and fill
LIMIT_ROOM = 0.005  # a limit buy may execute this far above the ask it was priced on
LOOKUP_DELAYS = (0.5, 1.0, 2.0, 4.0)  # seconds between lookups of an order in doubt
ORDER_PATIENCE = 120.0  # seconds after sending before an order Binance does not know failed
PRICE_ATTEMPTS = 3  # a paper quote lost to a network blip is asked again
LOCKED = "other orders lock the rest, which has"  # why a stop covers part of a position
FAILURE_ALERT_SECONDS = 3600.0  # a symbol's failed orders are sent at most this often


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

    async def before_event(self, portfolio: Portfolio) -> None:
        """Book what the exchange did since the last look, before the event values the book."""


def order_id(now: datetime, symbol: str, quantity: float, attempt: int = 1, tag: str = "") -> str:
    """Client order id from the session tag, bar event, symbol, and side; a repeat decision
    adds `r<n>`."""
    suffix = "" if attempt == 1 else f"r{attempt}"
    head = f"tb{tag}{to_millis(now)}{symbol}"[: 35 - len(suffix)]
    return f"{head}{'B' if quantity > 0 else 'S'}{suffix}"


def session_tag(ledger: Ledger) -> str:
    """Four hex digits kept in the ledger: two sessions on one account tell their orders
    apart by it, and neither cancels the other's stops."""
    tag = ledger.get_meta(TAG_META)
    if tag is None:
        tag = uuid4().hex[:4]
        ledger.set_meta(TAG_META, tag)
    return tag


def _sorted(orders: Mapping[str, float]) -> list[tuple[str, float]]:
    return sorted(orders.items(), key=lambda item: item[1])  # sells first fund buys


def limit_problem(ask: float, close: float, pct: float) -> str:
    """Why a buy at this ask pays too much over the close it was decided on, or ""."""
    if ask <= close * (1 + pct):
        return ""
    if not close > 0:
        return "no close to price it on"
    return (
        f"the ask {price_text(ask)} is {ask / close - 1:.1%} above the close "
        f"{price_text(close)} it was decided on, beyond buy_limit_pct {pct:g}"
    )


class FailureAlerts:
    """A symbol's failed order is sent once an hour: a halt tries its sale again within
    minutes, and an outage would otherwise bury every other alert."""

    def __init__(self, notifier: Notifier) -> None:
        self.notifier = notifier
        self.said: dict[str, float] = {}  # symbol -> when its last failure was sent

    async def failed(self, symbol: str, text: str) -> None:
        if time.monotonic() - self.said.get(symbol, -FAILURE_ALERT_SECONDS) < FAILURE_ALERT_SECONDS:
            log.warning("failure_not_sent_again", symbol=symbol, text=text)
            return
        self.said[symbol] = time.monotonic()
        await self.notifier.send(f"{text} (more failures of {symbol} this hour go to the log)")

    def worked(self, symbol: str) -> None:
        self.said.pop(symbol, None)  # the next failure is news again


class PaperExecutor:
    """Fills at once at the current book price through the simulated broker."""

    def __init__(
        self,
        broker: SimulatedBroker,
        prices: PriceSource,
        ledger: Ledger,
        notifier: Notifier,
        clock: Callable[[], datetime],
        label: str = "paper",
        *,
        buy_limit_pct: float = 0.0,
    ) -> None:
        self.broker = broker
        self.prices = prices
        self.ledger = ledger
        self.notifier = notifier
        self.clock = clock
        self.label = label
        self.buy_limit_pct = buy_limit_pct
        self.failures = FailureAlerts(notifier)

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
                await self.failures.failed(
                    symbol, f"[{self.label}] price lookup failed for {symbol}: {exc!r}"
                )
                continue
            if quantity > 0 and self.buy_limit_pct:
                problem = limit_problem(reference, marks.get(symbol, 0.0), self.buy_limit_pct)
                if problem:
                    await _skip_buy(
                        self.ledger, self.notifier, self.label, now, symbol, quantity, problem
                    )
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
            self.failures.worked(symbol)
            await self.notifier.send(_fill_text(self.label, fill, portfolio.equity(marks)))
            fills.append(fill)
        return fills

    async def after_event(self, portfolio: Portfolio, marks: Mapping[str, float]) -> None:
        return None

    async def before_event(self, portfolio: Portfolio) -> None:
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
        buy_limit_pct: float = 0.0,
        market_marks: bool = True,
    ) -> None:
        self.spot = spot
        self.ledger = ledger
        self.notifier = notifier
        self.clock = clock
        self.fee_rate = fee_rate
        self.protective_stop_pct = protective_stop_pct
        self.label = label
        self.buy_limit_pct = buy_limit_pct
        # Whether the closes come from the market the orders go to: testnet has prices of
        # its own, so its limits start from its own ask.
        self.market_marks = market_marks
        self.unprotected: set[str] = set()  # held symbols whose stop is missing
        self.alerted: set[str] = set()  # stop failures already sent, until one works
        self.statuses: dict[str, str] = {}  # each symbol's Binance status as last reported
        self.partial: dict[str, float] = {}  # held beyond a stop: other orders lock it
        self.stopless: set[str] = set()  # their stop was cancelled and not yet replaced
        self.missing_since: dict[str, datetime] = {}  # client id -> when it was sent
        self.skipped: set[str] = set()  # orders in doubt on pairs no longer traded, logged once
        self.tag = session_tag(ledger)
        self.stop_prefix = f"{STOP_PREFIX}{self.tag}"
        self.failures = FailureAlerts(notifier)

    async def execute(
        self,
        orders: Mapping[str, float],
        now: datetime,
        portfolio: Portfolio,
        marks: Mapping[str, float],
    ) -> list[Fill]:
        fills = []
        await self.check_rules()  # a filter Binance changed, a pair it paused or resumed
        doubtful = await self._settle_doubts(set(orders), portfolio)
        for symbol, quantity in _sorted(orders):
            if symbol in doubtful:
                # Its book was stale when this order was planned; the next bar decides again.
                self.ledger.add_order(now, symbol, quantity, "skipped", "an earlier order in doubt")
                await self.notifier.send(
                    f"[{self.label}] {symbol}: an earlier order was in doubt; no order this bar"
                )
                continue
            if quantity > 0 and self._buy_in_doubt():
                # Its cost is still in the book's cash: a buy now could spend the owner's.
                self.ledger.add_order(now, symbol, quantity, "skipped", "a buy in doubt")
                await self.notifier.send(
                    f"[{self.label}] {symbol}: a buy whose result is not known yet may have "
                    "spent the book's cash; no buy this bar"
                )
                continue
            close = marks.get(symbol, 0.0)
            fill = await self._execute_order(symbol, quantity, now, portfolio, close)
            if fill is not None:
                self.failures.worked(symbol)
                fills.append(fill)
                await self.notifier.send(_fill_text(self.label, fill, portfolio.equity(marks)))
        return fills

    async def _execute_order(
        self, symbol: str, quantity: float, now: datetime, portfolio: Portfolio, close: float
    ) -> Fill | None:
        """Send one order and book it; a failure is recorded and alerted, never raised.

        A refusal for the symbol's filters means the cached rules are out of date: they are
        reloaded and the order is tried once more, instead of waiting for the next bar.
        """
        for retry in (False, True):
            client_id = self._new_id(now, symbol, quantity)
            try:
                return await self._execute_one(symbol, quantity, now, portfolio, client_id, close)
            except OrderUnknown as exc:
                log.error("order_unknown", symbol=symbol, client_order_id=client_id)
                self.ledger.add_order(now, symbol, quantity, "unknown", str(exc), client_id)
                await self.notifier.send(
                    f"[{self.label}] order state unknown for {symbol}; it is looked up again "
                    f"before the next reconciliation"
                )
                return None
            except (BinanceError, httpx.HTTPError) as exc:
                self.ledger.add_order(now, symbol, quantity, "failed", repr(exc), client_id)
                if not retry and isinstance(exc, BinanceError) and exc.code in RULE_ERRORS:
                    log.warning("order_rules_outdated", symbol=symbol, error=str(exc))
                    await self.check_rules()
                    continue
                log.error("order_failed", symbol=symbol, quantity=quantity, error=repr(exc))
                await self.failures.failed(
                    symbol, f"[{self.label}] order failed for {symbol}: {exc}"
                )
                return None
            except Exception as exc:  # unexpected: the order may be out, so it is looked up
                log.exception("order_error", symbol=symbol, client_order_id=client_id)
                self.ledger.add_order(now, symbol, quantity, "unknown", repr(exc), client_id)
                await self.notifier.send(
                    f"[{self.label}] order error for {symbol}: {exc!r}; it is looked up again "
                    f"before the next reconciliation"
                )
                return None
        return None

    async def _settle_doubts(self, symbols: set[str], portfolio: Portfolio) -> set[str]:
        """Look up orders in doubt for these symbols; return the symbols that had one.

        An order that turns out executed changes the book the new order was planned on,
        and one still unknown leaves the book unknown, so either way the symbol waits; one
        that never executed changed nothing.
        """
        doubtful = set()
        for record in self.ledger.unresolved_orders():
            if record.symbol not in symbols or not self._listed(record.symbol):
                continue
            try:
                booked = await self._resolve(record, portfolio)
            except Exception as exc:  # asked again before the next reconciliation
                log.warning("order_lookup_failed", symbol=record.symbol, error=repr(exc))
                booked = 0
            still = any(r.client_id == record.client_id for r in self.ledger.unresolved_orders())
            if booked or still:
                doubtful.add(record.symbol)
        return doubtful

    def _buy_in_doubt(self) -> bool:
        return any(
            r.quantity > 0 and self._listed(r.symbol) for r in self.ledger.unresolved_orders()
        )

    def _new_id(self, now: datetime, symbol: str, quantity: float) -> str:
        """A second decision at the same close (a late stream) must not reuse an id."""
        attempt = 1
        while self.ledger.client_id_used(
            client_id := order_id(now, symbol, quantity, attempt, self.tag)
        ):
            attempt += 1
        return client_id

    async def _execute_one(
        self,
        symbol: str,
        quantity: float,
        now: datetime,
        portfolio: Portfolio,
        client_id: str,
        close: float,
    ) -> Fill | None:
        if not self.spot.trading(symbol):
            status = self.spot.status[symbol]
            self.ledger.add_order(now, symbol, quantity, "skipped", f"not trading ({status})")
            return None
        rules = self.spot.rules[symbol]
        if quantity < 0:
            await self.cancel_stops(symbol, portfolio)  # stops lock the base balance
        balances = await self.spot.balances()
        reference = await self.spot.book_price(symbol, 1 if quantity > 0 else -1)
        if not reference > 0:
            self.ledger.add_order(now, symbol, quantity, "skipped", "no price in the book")
            return None
        limit = None
        if quantity > 0 and self.buy_limit_pct:
            anchor = close if self.market_marks else reference
            problem = limit_problem(reference, anchor, self.buy_limit_pct)
            if problem:
                await _skip_buy(
                    self.ledger, self.notifier, self.label, now, symbol, quantity, problem
                )
                return None
            cap = anchor * (1 + self.buy_limit_pct)
            limit = rules.round_price(min(cap, reference * (1 + LIMIT_ROOM)))
        if quantity > 0:
            # Spend neither more than the account holds nor more than the book's own cash:
            # with budget ownership the rest of the account is the owner's money.
            free = balances[rules.quote].free if rules.quote in balances else 0.0
            spendable = max(0.0, min(free, portfolio.cash))
            if limit is None:
                price = reference * (1 + self.fee_rate + BUY_MARGIN)
            else:  # Binance holds quantity times the limit while it is sent
                price = limit * (1 + self.fee_rate)
            quantity = min(quantity, spendable / price)
        else:
            # Never sell more than the book holds: in budget mode the rest is the owner's,
            # and a stop booked by cancel_stops above may have sold part of it already.
            free = balances[rules.base].free if rules.base in balances else 0.0
            quantity = max(quantity, -max(portfolio.position(symbol), 0.0))
            quantity = max(rules.round_quantity(quantity), -rules.floor_quantity(free))
        quantity = rules.round_quantity(quantity)
        if rules.cap_quantity(quantity) != quantity:
            log.warning(
                "order_capped", symbol=symbol, quantity=quantity, largest=str(rules.max_qty)
            )
            quantity = rules.cap_quantity(quantity)  # the rest at the next decision
        if quantity == 0 or not rules.acceptable(quantity, reference):
            self.ledger.add_order(now, symbol, quantity, "skipped", "below exchange minimum")
            return None
        self.ledger.add_order(now, symbol, quantity, "pending", "", client_id)  # write-ahead
        order = await self._place(symbol, quantity, client_id, limit)
        fill = await self._book(order, rules, now, quantity, reference, portfolio, client_id)
        await self._note_short(order, quantity, rules, limit)
        return fill

    async def _note_short(
        self, order: Order, quantity: float, rules: SymbolRules, limit: float | None = None
    ) -> None:
        """Say when Binance ended an order before all of it executed: the price moved past
        a buy's limit, too little in the book, or the owner's own order on the other side
        (self-trade prevention)."""
        if abs(quantity) - order.executed_qty < float(rules.step_size) / 2:
            return
        side = "BUY" if quantity > 0 else "SELL"
        log.warning(
            "order_short", symbol=order.symbol, status=order.status, executed=order.executed_qty
        )
        if limit is not None and order.status == "EXPIRED":
            await self.notifier.send(
                f"[{self.label}] the {order.symbol} buy executed {order.executed_qty:.6f} of "
                f"{abs(quantity):.6f} within its limit {price_text(limit)}: the price moved "
                "past it; the rest waits for the next decision"
            )
            return
        if order.executed_qty:
            done = f"after {order.executed_qty:.6f} of {abs(quantity):.6f}; the rest waits"
        else:
            done = "with nothing executed; it waits"
        why = ""
        if order.status == "EXPIRED_IN_MATCH":
            why = ": it met a resting order of this account (self-trade prevention)"
        await self.notifier.send(
            f"[{self.label}] Binance ended the {side} order for {order.symbol} "
            f"({order.status}{why}) {done} for the next decision"
        )

    async def _place(
        self, symbol: str, quantity: float, client_id: str, limit: float | None = None
    ) -> Order:
        """Send the order: at the market, or with a limit as an IOC order. When the answer
        leaves its fate open, ask the exchange by client id.

        Raises OrderUnknown when the lookups find nothing: an order whose send status was
        unknown can still appear later, so settle() asks again before deciding it failed.
        """
        sent = self.clock()
        try:
            if limit is not None:
                return await self.spot.limit_ioc_order(symbol, quantity, limit, client_id)
            return await self.spot.market_order(symbol, quantity, client_id)
        except BinanceError as exc:
            if not exc.uncertain:
                raise  # rejected: nothing executed
            cause: Exception = exc
        except httpx.HTTPError as exc:
            cause = exc
        log.warning("order_send_uncertain", symbol=symbol, error=repr(cause))
        state = "not found yet"
        for delay in LOOKUP_DELAYS:
            await asyncio.sleep(delay)
            try:  # one try each: these lookups are the retries, under the event lock
                order = await self.spot.get_order(symbol, client_id, attempts=1)
            except (BinanceError, httpx.HTTPError) as exc:
                log.warning("order_lookup_failed", symbol=symbol, error=repr(exc))
                continue
            if order is not None and order.done:
                log.warning("order_recovered", symbol=symbol, client_order_id=client_id)
                return order
            if order is not None:  # booked now, what executes later would be lost
                state = f"still working ({order.status})"
        self.missing_since[client_id] = sent
        raise OrderUnknown(f"{symbol} {client_id}: sent, then {cause!r}; {state}")

    async def _book(
        self,
        order: Order,
        rules: SymbolRules,
        time: datetime,
        requested: float,
        reference: float,
        portfolio: Portfolio,
        client_id: str,
        executed: datetime | None = None,
    ) -> Fill | None:
        """Book an order's executions, as of `executed` (else now)."""
        if order.executed_qty == 0:
            self.ledger.add_order(
                time, order.symbol, requested, "unfilled", order.status, client_id
            )
            return None
        fill = await self._to_fill(order, rules, executed)
        with self.ledger.atomic():  # a crash between the two would book it twice
            self.ledger.add_fill(fill, reference)
            self.ledger.add_order(time, order.symbol, fill.quantity, "filled", "", client_id)
        portfolio.apply(fill)
        return fill

    async def _to_fill(
        self, order: Order, rules: SymbolRules, executed: datetime | None = None
    ) -> Fill:
        """Commission in the base asset shrinks the quantity; every commission becomes quote fee.

        An order read back by lookup carries no executions, so they are fetched.
        """
        trades: list[TradeFill] | None = None
        if not order.fills:
            try:
                trades = await self.spot.my_trades(order.symbol, order.order_id)
            except (BinanceError, httpx.HTTPError) as exc:
                log.warning("trades_unavailable", symbol=order.symbol, error=repr(exc))
            if not trades:  # unreadable, or not listed yet: estimate at the configured rate
                log.warning("commission_estimated", symbol=order.symbol)
                trades = [self._estimated_commission(order, rules)]
        fill, commissions = aggregate_fill(order, executed or self.clock(), trades)
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

    def _estimated_commission(self, order: Order, rules: SymbolRules) -> TradeFill:
        """Where Binance takes it by default: from the coin bought, or the quote sold for."""
        if order.side == "BUY":
            return TradeFill(0.0, 0.0, order.executed_qty * self.fee_rate, rules.base)
        return TradeFill(0.0, 0.0, order.quote_qty * self.fee_rate, rules.quote)

    # symbol rules and status

    async def check_rules(self) -> None:
        """Reload the symbol rules when due, and say when Binance pauses or resumes a pair."""
        before = dict(self.spot.rules)
        try:
            await self.spot.refresh_rules()
        except (BinanceError, httpx.HTTPError) as exc:  # the old rules stay; tried next round
            log.warning("rules_refresh_failed", error=repr(exc))
        for symbol, rules in self.spot.rules.items():
            if symbol in before and _stop_fit(rules) != _stop_fit(before[symbol]):
                self.unprotected.add(symbol)  # a stop sized on the old step or tick: refit it
        await self.report_status()

    def _listed(self, symbol: str) -> bool:
        """Configured and still listed: Binance answers -1121 for any call on a pair it
        stopped listing, which would fail every reconciliation."""
        return symbol in self.spot.rules and self.spot.status.get(symbol) != UNLISTED

    def in_doubt(self) -> bool:
        """An order Binance may still list: its trade would look like a transfer now."""
        return any(self._listed(r.symbol) for r in self.ledger.unresolved_orders())

    async def report_status(self) -> None:
        for symbol in sorted(self.spot.rules):
            status = self.spot.status.get(symbol, TRADING)
            if status == self.statuses.get(symbol, TRADING):
                continue
            self.statuses[symbol] = status
            if status == TRADING:
                self.unprotected.add(symbol)  # refit its stop to what was booked meanwhile
                text = f"[{self.label}] Binance trades {symbol} again; its orders and stops resume"
            else:
                text = (
                    f"[{self.label}] Binance does not trade {symbol} now ({status}): no orders "
                    "or stops for it until it does"
                )
            log.warning("symbol_status", symbol=symbol, status=status)
            self.ledger.add_event(self.clock(), "warning", text)
            await self.notifier.send(text)

    # outside events: orders in doubt and stops that executed

    async def settle(self, portfolio: Portfolio) -> int:
        """Book what happened on the exchange between events; return the fills booked."""
        booked = 0
        for record in self.ledger.unresolved_orders():
            if not self._listed(record.symbol):  # no longer traded: the operator decides
                if record.client_id not in self.skipped:
                    self.skipped.add(record.client_id)
                    log.warning("unresolved_order_skipped", symbol=record.symbol)
                continue
            booked += await self._resolve(record, portfolio)
        for symbol in self.spot.rules:
            if self._listed(symbol):
                booked += await self._settle_stop(symbol, portfolio)
        return booked

    async def before_event(self, portfolio: Portfolio) -> None:
        """Book stops that executed since the last look: the guard and the strategies must
        not value coins a stop already sold."""
        for symbol in self.spot.rules:
            if not self._listed(symbol):
                continue
            try:
                await self._settle_stop(symbol, portfolio)
            except (BinanceError, httpx.HTTPError) as exc:  # after_event settles it later
                log.warning("stop_lookup_failed", symbol=symbol, error=repr(exc))

    async def _resolve(self, record: OrderRecord, portfolio: Portfolio) -> int:
        order = await self.spot.get_order(record.symbol, record.client_id)
        if order is None:
            # Binance can list an order whose send timed out only later: wait a while,
            # from the send, or for a row a crash left, from its event (never after it).
            first = self.missing_since.setdefault(record.client_id, record.time)
            if (self.clock() - first).total_seconds() < ORDER_PATIENCE:
                return 0
            self.missing_since.pop(record.client_id, None)
            self.ledger.add_order(
                record.time,
                record.symbol,
                record.quantity,
                "failed",
                "not on exchange",
                record.client_id,
            )
            log.warning("order_resolved", symbol=record.symbol, outcome="never executed")
            await self.notifier.send(
                f"[{self.label}] the {record.symbol} order in doubt never reached the exchange; "
                "nothing was traded"
            )
            return 0
        if not order.done:
            return 0  # still working; asked again next time
        self.missing_since.pop(record.client_id, None)
        rules = self.spot.rules[record.symbol]
        price = order.quote_qty / order.executed_qty if order.executed_qty else 0.0
        # Booked when it executed, which may be long before this lookup (a crash).
        when = min(order.updated, self.clock()) if order.updated else None
        fill = await self._book(
            order, rules, record.time, record.quantity, price, portfolio, record.client_id, when
        )
        log.warning("order_resolved", symbol=record.symbol, client_order_id=record.client_id)
        if fill is None:
            await self.notifier.send(
                f"[{self.label}] the {record.symbol} order in doubt ended unfilled "
                f"({order.status}); nothing was traded"
            )
            return 0
        self.unprotected.add(record.symbol)  # its stop fits the book again, even if this
        # round fails before the stops are replaced
        await self.notifier.send(
            f"[{self.label}] booked an order whose result was not recorded: {_fill_summary(fill)}"
        )
        return 1

    async def _settle_stop(self, symbol: str, portfolio: Portfolio) -> int:
        """Book the executed part of this bot's protective stop; forget it once it is done.

        Returns 1 when it booked a fill.
        """
        state = self._stop_state(symbol)
        if state is None:
            return 0
        order = await self.spot.get_order(symbol, state["id"])
        if order is None:  # never reached the exchange
            self._set_stop_state(symbol, None)
            self.unprotected.add(symbol)
            return 0
        quantity = order.executed_qty - state["qty"]
        fill = None
        if quantity > 1e-12:
            quote = order.quote_qty - state["quote"]
            price = quote / quantity
            fee = quote * self.fee_rate  # a stop's commission is not in its order record
            # Booked when it executed, which may be long before this lookup (a bot down).
            when = min(order.updated, self.clock()) if order.updated else self.clock()
            fill = Fill(when, symbol, -quantity, price, fee)
            state = StopState(id=state["id"], qty=order.executed_qty, quote=order.quote_qty)
        with self.ledger.atomic():  # booked once, even across a crash
            if fill is not None:
                self.ledger.add_fill(fill, order.stop_price or fill.price)
                self.ledger.add_order(
                    fill.time, symbol, fill.quantity, "filled", "protective stop", state["id"]
                )
            self._set_stop_state(symbol, None if order.done else state)
        if fill is not None:
            portfolio.apply(fill)
        if order.done and portfolio.position(symbol) > 0:
            self.unprotected.add(symbol)  # cancelled, expired, or only partly executed
        if fill is None:
            return 0
        log.warning("stop_executed", symbol=symbol, quantity=quantity, price=fill.price)
        await self.notifier.send(
            f"[{self.label}] protective stop sold {quantity:.6f} {symbol} "
            f"@ {price_text(fill.price)}; the strategy decides again at the next close and may "
            f"buy back (`tbot halt` keeps it out)"
        )
        return 1

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
        count = await self._cancel_own_stops(symbol)
        await self._settle_stop(symbol, portfolio)
        return count

    async def _cancel_own_stops(self, symbol: str) -> int:
        count = 0
        state = self._stop_state(symbol)
        tracked = state["id"] if state is not None else None  # also one from before tags
        for order in await self.spot.open_orders(symbol):
            own = order.client_order_id
            if not (own.startswith(self.stop_prefix) or own == tracked):
                continue  # another session's stop, or the owner's order
            try:
                await self.spot.cancel_order(symbol, order.order_id)
            except BinanceError as exc:
                if exc.code != NOTHING_TO_CANCEL:
                    raise
                log.info("stop_already_gone", symbol=symbol, order_id=order.order_id)
                continue
            count += 1
            self.stopless.add(symbol)
        return count

    async def after_event(self, portfolio: Portfolio, marks: Mapping[str, float]) -> None:
        """Replace every stop at the latest close."""
        for symbol in self.spot.rules:
            await self._protect(symbol, portfolio, marks)

    async def protect(self, portfolio: Portfolio, marks: Mapping[str, float]) -> None:
        """Place stops that are missing: a failed placement, or a stop that ended early."""
        for symbol in sorted(self.unprotected):
            await self._protect(symbol, portfolio, marks)

    @staticmethod
    def _stoppable(rules: SymbolRules, quantity: float, price: float) -> bool:
        """Whether a stop for this quantity passes the exchange's minimums."""
        rounded = rules.round_quantity(quantity)
        return rounded > 0 and rules.acceptable(rounded, price)

    async def _check_cover(
        self, symbol: str, state: StopState, portfolio: Portfolio, marks: Mapping[str, float]
    ) -> None:
        """Flag coins beyond what a triggered, still working stop is going to sell."""
        try:
            order = await self.spot.get_order(symbol, state["id"])
        except (BinanceError, httpx.HTTPError) as exc:
            log.warning("stop_lookup_failed", symbol=symbol, error=repr(exc))
            return
        if order is None or order.done:
            return  # settle books it, and a new stop covers the whole position
        uncovered = portfolio.position(symbol) - (order.orig_qty - state["qty"])
        price = order.stop_price or marks.get(symbol, 0.0)
        if not self._stoppable(self.spot.rules[symbol], uncovered, price):
            return
        self.unprotected.add(symbol)
        if symbol not in self.alerted:
            self.alerted.add(symbol)
            await self.notifier.send(
                f"[{self.label}] {symbol}: a triggered stop is still filling; the "
                f"{uncovered:.6f} bought since has no exchange stop until it is done"
            )

    async def _protect(self, symbol: str, portfolio: Portfolio, marks: Mapping[str, float]) -> None:
        if not self.protective_stop_pct:
            self.unprotected.discard(symbol)
            self.partial.pop(symbol, None)
            if self._stop_state(symbol) is not None:  # turned off: a stop from before goes
                await self._drop_stop(symbol, portfolio)
            return
        if not self.spot.trading(symbol):
            return  # no orders until Binance trades it again; a stop in place stays
        state = self._stop_state(symbol)
        if state is not None and state["qty"] > 0:
            # Triggered and partly filled: the rest is still working; leave it. Coins
            # bought since then are beyond it, without a stop until it is done.
            await self._check_cover(symbol, state, portfolio, marks)
            return
        if any(r.symbol == symbol and r.quantity < 0 for r in self.ledger.unresolved_orders()):
            # A sell in doubt may have sold the book's coins: free coins can be the owner's.
            self.unprotected.add(symbol)  # placed once reconciliation settles it
            return
        cancelled = False
        try:
            bid = 0.0
            if portfolio.position(symbol) > 0 and symbol in marks:
                bid = await self.spot.book_price(symbol, -1)
                level = marks[symbol] * (1 - self.protective_stop_pct)
                if state is not None and 0 < bid <= level:
                    # The price fell through the new level since the close, and Binance
                    # refuses a stop that would trigger at once: the one working stays.
                    log.warning("stop_kept", symbol=symbol, level=level, bid=bid)
                    await self._note_kept(symbol, state, portfolio, level)
                    return
            await self._cancel_own_stops(symbol)
            cancelled = True  # from here on, a failure leaves the position without a stop
            await self._settle_stop(symbol, portfolio)
            for retry in (False, True):
                try:
                    # Again at most the largest order Binance takes, if that was the refusal.
                    await self._place_stop(symbol, portfolio, marks, bid, capped=retry)
                    break
                except BinanceError as exc:
                    if retry or exc.code not in RULE_ERRORS:
                        raise
                    log.warning("stop_rules_outdated", symbol=symbol, error=str(exc))
                    await self.check_rules()  # a step or tick Binance changed: size it again
        except Exception as exc:  # retried at every reconciliation until it works
            log.error("stop_failed", symbol=symbol, error=repr(exc))
            self.unprotected.add(symbol)
            self.partial.pop(symbol, None)
            gone = cancelled or symbol in self.stopless  # here, or by a sale
            # A pair the book holds nothing of loses no protection: the log is enough.
            if symbol not in self.alerted and (gone or portfolio.position(symbol) > 0):
                self.alerted.add(symbol)
                left = (
                    "The position has no exchange stop"
                    if gone
                    else "The stop in place, if any, was not replaced and stays"
                )
                await self.notifier.send(
                    f"[{self.label}] protective stop failed for {symbol}: {exc}. {left}; "
                    "retrying at every reconciliation"
                )

    async def _note_kept(
        self, symbol: str, state: StopState, portfolio: Portfolio, level: float
    ) -> None:
        """A stop kept as it was covers the position it was placed for: flag coins bought
        since (this event's buy) as without a stop until the next refit."""
        try:
            order = await self.spot.get_order(symbol, state["id"])
        except (BinanceError, httpx.HTTPError) as exc:
            log.warning("stop_lookup_failed", symbol=symbol, error=repr(exc))
            return
        if order is None or order.done:
            return  # settle books it, and a new stop covers the whole position
        why = "the price fell through the new stop level, so the stop in place stays; the rest has"
        covered = order.orig_qty - order.executed_qty
        await self._note_partial(
            symbol, portfolio.position(symbol), covered, order.stop_price or level, why
        )

    async def _drop_stop(self, symbol: str, portfolio: Portfolio) -> None:
        """Cancel a stop placed while stops were on, and book what it sold first."""
        try:
            if await self.cancel_stops(symbol, portfolio):
                await self.notifier.send(
                    f"[{self.label}] protective stops are off (protective_stop_pct 0): "
                    f"cancelled the exchange stop of {symbol}"
                )
        except Exception as exc:  # tried again after the next event
            log.warning("stop_cancel_failed", symbol=symbol, error=repr(exc))

    async def _place_stop(
        self,
        symbol: str,
        portfolio: Portfolio,
        marks: Mapping[str, float],
        bid: float = 0.0,
        *,
        capped: bool = False,
    ) -> None:
        """A stop below the last close; below the bid when the price fell through that
        since the close (Binance refuses a stop that would trigger at once). `capped` sizes
        it at most the largest market order Binance takes."""
        rules = self.spot.rules[symbol]
        position = portfolio.position(symbol)
        if position <= 0 or symbol not in marks:
            self._covered(symbol)
            return
        balances = await self.spot.balances()
        balance = balances.get(rules.base)
        free = balance.free if balance is not None else 0.0
        stop = marks[symbol] * (1 - self.protective_stop_pct)
        if 0 < bid <= stop:
            stop = bid * (1 - self.protective_stop_pct)
        stop = rules.round_price(stop)
        if not self._stoppable(rules, position, stop):
            self._covered(symbol)  # dust: nothing an exchange stop can hold
            return
        held = min(rules.round_quantity(position), rules.floor_quantity(free))
        quantity = rules.cap_quantity(held) if capped else held
        if not self._stoppable(rules, held, stop):
            locked = balance.locked if balance is not None else 0.0
            if locked < float(rules.step_size):
                # No order holds the coins: the book is above the account by less than
                # reconciliation corrects, and the account has nothing a stop can hold.
                log.warning("stop_nothing_to_hold", symbol=symbol, free=free, book=position)
                self._covered(symbol)
                return
            raise ValueError(
                f"only {free:.8f} of the {position:.8f} held is free; other orders lock the rest"
            )
        client_id = f"{self.stop_prefix}{symbol}{uuid4().hex[:12]}"
        self._set_stop_state(symbol, StopState(id=client_id, qty=0.0, quote=0.0))
        await self.spot.stop_loss_order(symbol, quantity, stop, client_id)
        self.unprotected.discard(symbol)
        self.stopless.discard(symbol)
        why = LOCKED if quantity == held else "Binance takes no larger order, so the rest has"
        await self._note_partial(symbol, position, quantity, stop, why)
        if symbol in self.alerted:
            self.alerted.discard(symbol)
            await self.notifier.send(f"[{self.label}] protective stop for {symbol} placed")
        log.info("stop_placed", symbol=symbol, quantity=quantity, stop=stop)

    def _covered(self, symbol: str) -> None:
        """Nothing for a stop to hold: no alert and no flag for this symbol."""
        self.unprotected.discard(symbol)
        self.stopless.discard(symbol)
        self.alerted.discard(symbol)
        self.partial.pop(symbol, None)

    async def _note_partial(
        self,
        symbol: str,
        position: float,
        quantity: float,
        price: float,
        why: str = LOCKED,
    ) -> None:
        """Flag coins beyond a placed stop; it is not replaced for them between events."""
        uncovered = position - quantity
        if not self._stoppable(self.spot.rules[symbol], uncovered, price):
            self.partial.pop(symbol, None)
            return
        log.warning("stop_partial", symbol=symbol, quantity=quantity, position=position)
        if symbol not in self.partial:
            await self.notifier.send(
                f"[{self.label}] {symbol}: the stop covers {quantity:.6f} of {position:.6f}; "
                f"{why} no exchange stop"
            )
        self.partial[symbol] = uncovered


class BinanceBookTicker:
    """Paper price source: the public book ticker."""

    URL = "https://data-api.binance.vision/api/v3/ticker/bookTicker"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def price(self, symbol: str, side: int) -> float:
        for attempt in range(PRICE_ATTEMPTS):
            try:
                response = await self.client.get(self.URL, params={"symbol": symbol}, timeout=10.0)
                if response.status_code < 500 or attempt == PRICE_ATTEMPTS - 1:
                    break
            except httpx.TransportError:
                if attempt == PRICE_ATTEMPTS - 1:
                    raise
            await asyncio.sleep(2**attempt)
        response.raise_for_status()
        data = response.json()
        return float(data["askPrice"] if side > 0 else data["bidPrice"])


async def _skip_buy(
    ledger: Ledger,
    notifier: Notifier,
    label: str,
    now: datetime,
    symbol: str,
    quantity: float,
    problem: str,
) -> None:
    log.warning("buy_beyond_limit", symbol=symbol, problem=problem)
    ledger.add_order(now, symbol, quantity, "skipped", problem)
    await notifier.send(
        f"[{label}] no {symbol} buy this bar: {problem}; the next close decides again"
    )


def _stop_fit(rules: SymbolRules) -> tuple[object, ...]:
    """What a placed stop was sized on: Binance moves the largest market order often, and
    a change of it alone needs no new stop."""
    return (
        rules.base,
        rules.quote,
        rules.tick_size,
        rules.step_size,
        rules.min_qty,
        rules.min_notional,
    )


def _fill_summary(fill: Fill) -> str:
    side = "BUY" if fill.quantity > 0 else "SELL"
    price = price_text(fill.price)
    return f"{side} {abs(fill.quantity):.6f} {fill.symbol} @ {price} fee {fill.fee:.2f}"


def _fill_text(label: str, fill: Fill, equity: float) -> str:
    return f"[{label}] {_fill_summary(fill)} | equity {equity:,.2f}"
