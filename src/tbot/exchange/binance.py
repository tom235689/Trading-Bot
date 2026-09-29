"""Minimal Binance spot adapter: signed REST for account, orders, and symbol rules."""

import asyncio
import hashlib
import hmac
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

from tbot.core.models import Fill
from tbot.core.timeframe import to_millis

log = structlog.get_logger(__name__)

PRODUCTION_URL = "https://api.binance.com"
TESTNET_URL = "https://testnet.binance.vision"
RETRY_STATUS = frozenset({418, 429, 500, 502, 503, 504})
ORDER_NOT_FOUND = -2013
NOTHING_TO_CANCEL = -2011
Params = Mapping[str, str | int | float]


class BinanceError(Exception):
    """Error response from Binance; code is from the JSON body, 0 for other failures."""

    def __init__(self, code: int, message: str, status: int = 0) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class SymbolRules:
    base: str
    quote: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal

    def round_quantity(self, quantity: float) -> float:
        """Round the magnitude down to the step size; the sign is kept."""
        magnitude = Decimal(str(abs(quantity))).quantize(self.step_size, rounding=ROUND_DOWN)
        return math.copysign(float(magnitude), quantity) if magnitude else 0.0

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


def aggregate_fill(order: Order, when: datetime) -> tuple[Fill | None, dict[str, float]]:
    """One Fill for a filled order plus commissions by asset.

    The Fill's fee is zero; the caller converts the commissions into the quote asset.
    """
    if order.executed_qty == 0:
        return None, {}
    quantity = order.executed_qty if order.side == "BUY" else -order.executed_qty
    price = order.quote_qty / order.executed_qty
    commissions: dict[str, float] = {}
    for fill in order.fills:
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
    ) -> None:
        self.client = client
        self.api_key = api_key
        self.secret = api_secret.encode()
        self.base_url = base_url.rstrip("/")
        self.clock = clock
        self.recv_window = recv_window
        self.retries = retries
        self.rules: dict[str, SymbolRules] = {}

    # transport

    def _sign(self, params: Params) -> str:
        stamped = {**params, "timestamp": to_millis(self.clock()), "recvWindow": self.recv_window}
        query = urlencode(stamped)
        signature = hmac.new(self.secret, query.encode(), hashlib.sha256).hexdigest()
        return f"{query}&signature={signature}"

    async def _request(
        self, method: str, path: str, params: Params | None = None, *, signed: bool = False
    ) -> Any:
        """Send a request, retrying rate limits and server errors.

        POST is never retried here: an order may have gone through, so callers
        look it up by client order id before trying again.
        """
        params = dict(params or {})
        headers = {"X-MBX-APIKEY": self.api_key}
        attempts = 1 if method == "POST" else self.retries
        for attempt in range(attempts):
            query = self._sign(params) if signed else urlencode(params)
            url = f"{self.base_url}{path}?{query}" if query else f"{self.base_url}{path}"
            response = await self.client.request(method, url, headers=headers, timeout=15.0)
            if response.status_code in RETRY_STATUS and attempt < attempts - 1:
                delay = float(response.headers.get("Retry-After", 2**attempt))
                log.warning("binance_retry", path=path, status=response.status_code, delay=delay)
                await asyncio.sleep(delay)
                continue
            if response.status_code >= 400:
                raise _error_from(response)
            return response.json()
        raise AssertionError("unreachable")

    # public

    async def server_time(self) -> datetime:
        data = await self._request("GET", "/api/v3/time")
        return datetime.fromtimestamp(data["serverTime"] / 1000, UTC)

    async def load_rules(self, symbols: list[str]) -> dict[str, SymbolRules]:
        query = json.dumps(sorted(symbols), separators=(",", ":"))
        data = await self._request("GET", "/api/v3/exchangeInfo", {"symbols": query})
        for info in data["symbols"]:
            if info["status"] != "TRADING":
                raise BinanceError(0, f"{info['symbol']} is not trading: {info['status']}")
            self.rules[info["symbol"]] = parse_rules(info)
        missing = set(symbols) - set(self.rules)
        if missing:
            raise BinanceError(0, f"unknown symbols: {sorted(missing)}")
        return self.rules

    async def book_price(self, symbol: str, side: int) -> float:
        data = await self._request("GET", "/api/v3/ticker/bookTicker", {"symbol": symbol})
        return float(data["askPrice"] if side > 0 else data["bidPrice"])

    async def last_price(self, symbol: str) -> float:
        data = await self._request("GET", "/api/v3/ticker/price", {"symbol": symbol})
        return float(data["price"])

    # account

    async def balances(self) -> dict[str, Balance]:
        data = await self._request("GET", "/api/v3/account", signed=True)
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
