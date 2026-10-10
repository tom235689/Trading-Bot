"""Minimal Binance spot adapter: signed REST for account, orders, and symbol rules."""

import asyncio
import hashlib
import hmac
import json
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

from tbot.core.models import Fill
from tbot.core.timeframe import to_millis
from tbot.data.http import retry_seconds

log = structlog.get_logger(__name__)

PRODUCTION_URL = "https://api.binance.com"
TESTNET_URL = "https://testnet.binance.vision"
RETRY_STATUS = frozenset({418, 429, 500, 502, 503, 504})
MAX_RETRY_WAIT = 30.0  # seconds; a longer Retry-After (an IP ban) fails at once
RATE_LIMITED = frozenset({418, 429})
RULES_MAX_AGE = 3600.0  # seconds; Binance changes filters and statuses of listed pairs
ORDER_NOT_FOUND = -2013
NOTHING_TO_CANCEL = -2011
TIMESTAMP_OUTSIDE_WINDOW = -1021
SEND_STATUS_UNKNOWN = -1007
INVALID_SYMBOL = -1121
FILTER_FAILURE = -1013
BAD_PRECISION = -1111
BAD_SIGNATURE = -1022
BAD_KEY_FORMAT = -2014
RULE_ERRORS = frozenset({FILTER_FAILURE, BAD_PRECISION})  # the cached rules may be out of date
KEY_ERRORS = frozenset({BAD_SIGNATURE, BAD_KEY_FORMAT})  # the key or secret is wrong
TRADING = "TRADING"
NOISE = Decimal("1e-6")  # of a step: closer than this to it is float rounding, not a remainder
Params = Mapping[str, str | int | float]


class BinanceError(Exception):
    """Error response from Binance; code is from the JSON body, 0 for other failures."""

    def __init__(self, code: int, message: str, status: int = 0) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status

    @property
    def uncertain(self) -> bool:
        """The request may have been executed: Binance says so for 5xx and -1007."""
        return self.status >= 500 or self.code == SEND_STATUS_UNKNOWN


@dataclass(frozen=True)
class SymbolRules:
    base: str
    quote: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal

    def round_quantity(self, quantity: float) -> float:
        """Round the magnitude down to the step size; the sign is kept.

        A float a hair below a step (0.7 + 0.1 = 0.7999999999999999) counts as that step:
        else selling a whole position would leave one step behind.
        """
        exact = Decimal(str(abs(quantity)))
        nearest = exact.quantize(self.step_size)
        if abs(nearest - exact) > self.step_size * NOISE:
            nearest = exact.quantize(self.step_size, rounding=ROUND_DOWN)
        return math.copysign(float(nearest), quantity) if nearest else 0.0

    def floor_quantity(self, quantity: float) -> float:
        """Round a balance down to the step size: never more than is there."""
        exact = Decimal(str(max(quantity, 0.0)))
        return float(exact.quantize(self.step_size, rounding=ROUND_DOWN))

    def round_price(self, price: float) -> float:
        return float(Decimal(str(price)).quantize(self.tick_size, rounding=ROUND_DOWN))

    def acceptable(self, quantity: float, price: float) -> bool:
        magnitude = Decimal(str(abs(quantity)))
        return magnitude >= self.min_qty and magnitude * Decimal(str(price)) >= self.min_notional

    def format_quantity(self, quantity: float) -> str:
        return format(Decimal(str(abs(quantity))).quantize(self.step_size), "f")

    def format_price(self, price: float) -> str:
        return format(Decimal(str(price)).quantize(self.tick_size), "f")


@dataclass(frozen=True)
class Balance:
    asset: str
    free: float
    locked: float

    @property
    def total(self) -> float:
        return self.free + self.locked


@dataclass(frozen=True)
class TradeFill:
    price: float
    quantity: float
    commission: float
    commission_asset: str


@dataclass(frozen=True)
class Order:
    symbol: str
    order_id: int
    client_order_id: str
    side: str
    type: str
    status: str
    executed_qty: float
    quote_qty: float  # cumulative quote spent or received
    stop_price: float | None = None
    fills: tuple[TradeFill, ...] = ()  # only on a FULL order response
    orig_qty: float = 0.0  # the size ordered

    @property
    def done(self) -> bool:
        return self.status in {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}


def parse_order(data: Mapping[str, Any]) -> Order:
    stop = float(data.get("stopPrice", 0) or 0)
    fills = tuple(
        TradeFill(float(f["price"]), float(f["qty"]), float(f["commission"]), f["commissionAsset"])
        for f in data.get("fills", [])
    )
    return Order(
        symbol=data["symbol"],
        order_id=int(data["orderId"]),
        client_order_id=data.get("clientOrderId", ""),
        side=data["side"],
        type=data["type"],
        status=data["status"],
        executed_qty=float(data.get("executedQty", 0)),
        quote_qty=float(data.get("cummulativeQuoteQty", 0)),
        stop_price=stop or None,
        fills=fills,
        orig_qty=float(data.get("origQty", 0)),
    )


def parse_rules(info: Mapping[str, Any]) -> SymbolRules:
    filters = {f["filterType"]: f for f in info["filters"]}
    notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
    return SymbolRules(
        base=info["baseAsset"],
        quote=info["quoteAsset"],
        tick_size=Decimal(filters["PRICE_FILTER"]["tickSize"]).normalize(),
        step_size=Decimal(filters["LOT_SIZE"]["stepSize"]).normalize(),
        min_qty=Decimal(filters["LOT_SIZE"]["minQty"]),
        min_notional=Decimal(notional.get("minNotional", "0")),
    )


def aggregate_fill(
    order: Order, when: datetime, trades: Sequence[TradeFill] | None = None
) -> tuple[Fill | None, dict[str, float]]:
    """One Fill for a filled order plus commissions by asset.

    Commissions come from `trades`, or from the order's own fills when not given.
    The Fill's fee is zero; the caller converts the commissions into the quote asset.
    """
    if order.executed_qty == 0:
        return None, {}
    quantity = order.executed_qty if order.side == "BUY" else -order.executed_qty
    price = order.quote_qty / order.executed_qty
    commissions: dict[str, float] = {}
    for fill in order.fills if trades is None else trades:
        commissions[fill.commission_asset] = (
            commissions.get(fill.commission_asset, 0.0) + fill.commission
        )
    return Fill(when, order.symbol, quantity, price, 0.0), commissions


class BinanceSpot:
    """One account on production or testnet. Every failure raises BinanceError."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        api_secret: str,
        *,
        base_url: str = PRODUCTION_URL,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        recv_window: int = 5000,
        retries: int = 4,
        resync: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        self.client = client
        self.api_key = api_key
        self.secret = api_secret.encode()
        self.base_url = base_url.rstrip("/")
        self.clock = clock
        self.recv_window = recv_window
        self.retries = retries
        self.resync = resync  # re-measures the clock after a -1021 timestamp error
        self.rules: dict[str, SymbolRules] = {}
        self.status: dict[str, str] = {}  # TRADING, or BREAK while Binance pauses a pair
        self.rules_time = -math.inf  # monotonic time of the last load
        self.paused_until = 0.0  # monotonic: Binance asked for a longer pause than a retry

    # transport

    def _sign(self, params: Params) -> str:
        stamped = {**params, "timestamp": to_millis(self.clock()), "recvWindow": self.recv_window}
        query = urlencode(stamped)
        signature = hmac.new(self.secret, query.encode(), hashlib.sha256).hexdigest()
        return f"{query}&signature={signature}"

    async def _request(
        self, method: str, path: str, params: Params | None = None, *, signed: bool = False
    ) -> Any:
        """Send a request, retrying lost connections, rate limits, and server errors.

        POST is never retried on these: an order may have gone through, so callers
        look it up by client order id. Any request rejected for its timestamp (-1021)
        was not executed, so it is signed again once after a clock resync. During a
        pause Binance asked for, nothing is sent: calling on makes a ban longer.
        """
        params = dict(params or {})
        headers = {"X-MBX-APIKEY": self.api_key}
        attempts = 1 if method == "POST" else self.retries
        attempt, resynced = 0, False
        while True:
            wait = self.paused_until - time.monotonic()
            if wait > 0:
                raise BinanceError(0, f"rate limited: Binance asked to wait {wait:.0f} s more", 429)
            query = self._sign(params) if signed else urlencode(params)
            url = f"{self.base_url}{path}?{query}" if query else f"{self.base_url}{path}"
            try:
                response = await self.client.request(method, url, headers=headers, timeout=15.0)
            except httpx.TransportError as exc:
                if method == "POST" or attempt >= attempts - 1:
                    raise
                log.warning("binance_retry", path=path, error=type(exc).__name__, delay=2**attempt)
                await asyncio.sleep(2**attempt)
                attempt += 1
                continue
            delay = retry_seconds(response.headers.get("Retry-After"), 2**attempt)
            if (
                response.status_code in RETRY_STATUS
                and attempt < attempts - 1
                and delay <= MAX_RETRY_WAIT  # waiting longer would stall every bar
            ):
                log.warning("binance_retry", path=path, status=response.status_code, delay=delay)
                await asyncio.sleep(delay)
                attempt += 1
                continue
            if response.status_code in RATE_LIMITED:
                self.paused_until = max(self.paused_until, time.monotonic() + delay)
            if response.status_code >= 400:
                error = _error_from(response)
                if error.code in RULE_ERRORS:
                    self.rules_time = -math.inf  # reload them before the next order
                if (
                    error.code == TIMESTAMP_OUTSIDE_WINDOW
                    and signed
                    and self.resync
                    and not resynced
                ):
                    log.warning("binance_clock_resync", path=path)
                    await self.resync()
                    resynced = True
                    continue
                raise error
            return response.json()

    # public

    async def server_time(self) -> datetime:
        data = await self._request("GET", "/api/v3/time")
        return datetime.fromtimestamp(data["serverTime"] / 1000, UTC)

    async def load_rules(self, symbols: list[str]) -> dict[str, SymbolRules]:
        """Filters and status of each symbol; a pair Binance pauses (BREAK) is listed with
        its status, one it does not list fails with INVALID_SYMBOL."""
        query = json.dumps(sorted(symbols), separators=(",", ":"))
        data = await self._request("GET", "/api/v3/exchangeInfo", {"symbols": query})
        infos = {info["symbol"]: info for info in data["symbols"]}
        missing = set(symbols) - set(infos)
        if missing:
            raise BinanceError(INVALID_SYMBOL, f"unknown symbols: {', '.join(sorted(missing))}")
        for symbol, info in infos.items():
            self.rules[symbol] = parse_rules(info)
            self.status[symbol] = str(info["status"])
        self.rules_time = time.monotonic()
        return self.rules

    async def refresh_rules(self, max_age: float = RULES_MAX_AGE) -> bool:
        """Reload the rules once they are max_age old, or an order failed a filter."""
        if not self.rules or time.monotonic() - self.rules_time < max_age:
            return False
        await self.load_rules(sorted(self.rules))
        return True

    def trading(self, symbol: str) -> bool:
        return self.status.get(symbol, TRADING) == TRADING

    async def book_price(self, symbol: str, side: int) -> float:
        data = await self._request("GET", "/api/v3/ticker/bookTicker", {"symbol": symbol})
        return float(data["askPrice"] if side > 0 else data["bidPrice"])

    async def last_price(self, symbol: str) -> float:
        data = await self._request("GET", "/api/v3/ticker/price", {"symbol": symbol})
        return float(data["price"])

    # account

    async def account(self) -> dict[str, Any]:
        """Raw account information: balances, canTrade, permissions."""
        data: dict[str, Any] = await self._request("GET", "/api/v3/account", signed=True)
        return data

    async def api_restrictions(self) -> dict[str, Any]:
        """What this API key may do, e.g. enableWithdrawals. Production only."""
        path = "/sapi/v1/account/apiRestrictions"
        data: dict[str, Any] = await self._request("GET", path, signed=True)
        return data

    async def balances(self) -> dict[str, Balance]:
        data = await self.account()
        result = {}
        for item in data["balances"]:
            balance = Balance(item["asset"], float(item["free"]), float(item["locked"]))
            if balance.total:
                result[balance.asset] = balance
        return result

    # orders

    async def market_order(self, symbol: str, quantity: float, client_order_id: str) -> Order:
        params: dict[str, str | int | float] = {
            "symbol": symbol,
            "side": "BUY" if quantity > 0 else "SELL",
            "type": "MARKET",
            "quantity": self.rules[symbol].format_quantity(quantity),
            "newClientOrderId": client_order_id,
            "newOrderRespType": "FULL",
        }
        return parse_order(await self._request("POST", "/api/v3/order", params, signed=True))

    async def stop_loss_order(
        self,
        symbol: str,
        quantity: float,
        stop_price: float,
        limit_price: float,
        client_order_id: str,
    ) -> Order:
        rules = self.rules[symbol]
        params: dict[str, str | int | float] = {
            "symbol": symbol,
            "side": "SELL",
            "type": "STOP_LOSS_LIMIT",
            "timeInForce": "GTC",
            "quantity": rules.format_quantity(quantity),
            "stopPrice": rules.format_price(stop_price),
            "price": rules.format_price(limit_price),
            "newClientOrderId": client_order_id,
            "newOrderRespType": "RESULT",  # a stop's default answer (ACK) omits side and status
        }
        return parse_order(await self._request("POST", "/api/v3/order", params, signed=True))

    async def get_order(self, symbol: str, client_order_id: str) -> Order | None:
        params = {"symbol": symbol, "origClientOrderId": client_order_id}
        try:
            data = await self._request("GET", "/api/v3/order", params, signed=True)
        except BinanceError as exc:
            if exc.code == ORDER_NOT_FOUND:
                return None
            raise
        return parse_order(data)

    async def my_trades(self, symbol: str, order_id: int) -> list[TradeFill]:
        """Executions of one order with their commissions."""
        params: dict[str, str | int | float] = {"symbol": symbol, "orderId": order_id}
        data = await self._request("GET", "/api/v3/myTrades", params, signed=True)
        return [
            TradeFill(
                float(t["price"]), float(t["qty"]), float(t["commission"]), t["commissionAsset"]
            )
            for t in data
        ]

    async def open_orders(self, symbol: str) -> list[Order]:
        data = await self._request("GET", "/api/v3/openOrders", {"symbol": symbol}, signed=True)
        return [parse_order(item) for item in data]

    async def cancel_order(self, symbol: str, order_id: int) -> Order:
        params: dict[str, str | int | float] = {"symbol": symbol, "orderId": order_id}
        return parse_order(await self._request("DELETE", "/api/v3/order", params, signed=True))

    async def cancel_all(self, symbol: str) -> int:
        try:
            data = await self._request(
                "DELETE", "/api/v3/openOrders", {"symbol": symbol}, signed=True
            )
        except BinanceError as exc:
            if exc.code == NOTHING_TO_CANCEL:
                return 0
            raise
        return len(data)


def _error_from(response: httpx.Response) -> BinanceError:
    try:
        body = response.json()
        code, message = int(body.get("code", 0)), str(body.get("msg", ""))
    except ValueError:
        code, message = 0, response.text[:200]
    return BinanceError(code, message, response.status_code)
