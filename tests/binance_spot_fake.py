"""In-memory fake of the Binance spot account and order endpoints."""

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl

import httpx

DEFAULT_RULES: dict[str, dict[str, str]] = {
    "BTCUSDT": {"tick": "0.01", "step": "0.00001", "minQty": "0.00001", "minNotional": "5"},
    "ETHUSDT": {"tick": "0.01", "step": "0.0001", "minQty": "0.0001", "minNotional": "5"},
}


def symbol_info(symbol: str, rules: dict[str, str], status: str = "TRADING") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "status": status,
        "baseAsset": symbol[:-4],
        "quoteAsset": symbol[-4:],
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": rules["tick"]},
            {"filterType": "LOT_SIZE", "stepSize": rules["step"], "minQty": rules["minQty"]},
            {"filterType": "NOTIONAL", "minNotional": rules["minNotional"]},
        ],
    }


@dataclass
class FakeSpot:
    """Balances, prices, and orders in memory. Verifies the API key and signature."""

    balances: dict[str, float]
    prices: dict[str, float]
    api_key: str = "key"
    secret: str = "secret"
    fee_rate: float = 0.001
    fee_asset: str | None = None  # None: base on buys, quote on sells; else e.g. BNB
    spread: float = 0.0001
    rules: dict[str, dict[str, str]] = field(default_factory=lambda: dict(DEFAULT_RULES))
    orders: list[dict[str, Any]] = field(default_factory=list)
    locked: dict[str, float] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    fail_next: list[httpx.Response] = field(default_factory=list)
    lose_next_order_response: bool = False  # place the order, then fail the response
    next_id: int = 1000

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def order_count(self, kind: str) -> int:
        return sum(1 for o in self.orders if o["type"] == kind)

    def trigger_stop(self, symbol: str) -> None:
        """Fill the open stop order of a symbol at its stop price, as the exchange would."""
        for order in self.orders:
            if (
                order["symbol"] == symbol
                and order["type"] == "STOP_LOSS_LIMIT"
                and order["status"] == "NEW"
            ):
                qty, price = float(order["origQty"]), float(order["stopPrice"])
                base, quote = symbol[:-4], symbol[-4:]
                self.locked[base] -= qty
                self.balances[quote] = self.balances.get(quote, 0.0) + qty * price * (
                    1 - self.fee_rate
                )
                order.update(
                    status="FILLED",
                    executedQty=order["origQty"],
                    cummulativeQuoteQty=f"{qty * price:.8f}",
                )
                return
        raise LookupError(f"no open stop order for {symbol}")

    # request handling

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_next:
            return self.fail_next.pop(0)
        path = request.url.path
        params = dict(parse_qsl(request.url.query.decode()))
        if path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": int(time.time() * 1000)})
        if path == "/api/v3/exchangeInfo":
            symbols = json.loads(params["symbols"])
            infos = [symbol_info(s, self.rules[s]) for s in symbols if s in self.rules]
            return httpx.Response(200, json={"symbols": infos})
        if path == "/api/v3/ticker/bookTicker":
            price = self.prices[params["symbol"]]
            return httpx.Response(
                200,
                json={
                    "symbol": params["symbol"],
                    "askPrice": f"{price * (1 + self.spread):.8f}",
                    "bidPrice": f"{price * (1 - self.spread):.8f}",
                },
            )
        if path == "/api/v3/ticker/price":
            return httpx.Response(
                200,
                json={"symbol": params["symbol"], "price": f"{self.prices[params['symbol']]:.8f}"},
            )
        error = self._check_signature(request, params)
        if error is not None:
            return error
        if path == "/api/v3/account":
            return httpx.Response(200, json={"balances": self._balance_rows()})
        if path == "/api/v3/order" and request.method == "POST":
            response = self._place(params)
            if self.lose_next_order_response and response.status_code == 200:
                self.lose_next_order_response = False
                raise httpx.ConnectError("response lost")
            return response
        if path == "/api/v3/order" and request.method == "GET":
            order = self._find(params.get("origClientOrderId"))
            if order is None:
                return _error(400, -2013, "Order does not exist.")
            return httpx.Response(200, json=order)
        if path == "/api/v3/order" and request.method == "DELETE":
            for order in self.orders:
                if str(order["orderId"]) == params.get("orderId") and order["status"] == "NEW":
                    self._cancel(order)
                    return httpx.Response(200, json=order)
            return _error(400, -2011, "Unknown order sent.")
        if path == "/api/v3/openOrders" and request.method == "GET":
            return httpx.Response(200, json=self._open(params["symbol"]))
        if path == "/api/v3/openOrders" and request.method == "DELETE":
            open_orders = self._open(params["symbol"])
            if not open_orders:
                return _error(400, -2011, "Unknown order sent.")
            for order in open_orders:
                self._cancel(order)
            return httpx.Response(200, json=open_orders)
        return httpx.Response(404, json={"code": -1, "msg": f"unhandled {request.method} {path}"})

    def _check_signature(
        self, request: httpx.Request, params: dict[str, str]
    ) -> httpx.Response | None:
        if request.headers.get("X-MBX-APIKEY") != self.api_key:
            return _error(401, -2015, "Invalid API-key, IP, or permissions for action.")
        signature = params.pop("signature", None)
        query = request.url.query.decode().rsplit("&signature=", 1)[0]
        expected = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        if signature != expected:
            return _error(400, -1022, "Signature for this request is not valid.")
        if "timestamp" not in params:
            return _error(400, -1102, "Mandatory parameter 'timestamp' was not sent.")
        return None

    def _balance_rows(self) -> list[dict[str, str]]:
        assets = set(self.balances) | set(self.locked)
        return [
            {
                "asset": a,
                "free": f"{self.balances.get(a, 0.0):.8f}",
                "locked": f"{self.locked.get(a, 0.0):.8f}",
            }
            for a in sorted(assets)
        ]

    def _find(self, client_order_id: str | None) -> dict[str, Any] | None:
        return next((o for o in self.orders if o["clientOrderId"] == client_order_id), None)

    def _open(self, symbol: str) -> list[dict[str, Any]]:
        return [o for o in self.orders if o["symbol"] == symbol and o["status"] == "NEW"]

    def _cancel(self, order: dict[str, Any]) -> None:
        order["status"] = "CANCELED"
        base = order["symbol"][:-4]
        self.locked[base] -= float(order["origQty"])
        self.balances[base] = self.balances.get(base, 0.0) + float(order["origQty"])

    def _place(self, params: dict[str, str]) -> httpx.Response:
        symbol, side, kind = params["symbol"], params["side"], params["type"]
        if self._find(params.get("newClientOrderId")) is not None:
            return _error(400, -2022, "Duplicate client order id.")
        rules = self.rules[symbol]
        qty = float(params["quantity"])
        base, quote = symbol[:-4], symbol[-4:]
        price = self.prices[symbol] * (1 + self.spread if side == "BUY" else 1 - self.spread)
        if qty < float(rules["minQty"]) or qty * price < float(rules["minNotional"]):
            return _error(400, -1013, "Filter failure: NOTIONAL")
        order: dict[str, Any] = {
            "symbol": symbol,
            "orderId": self.next_id,
            "clientOrderId": params.get("newClientOrderId", ""),
            "side": side,
            "type": kind,
            "status": "NEW",
            "origQty": f"{qty:.8f}",
            "executedQty": "0.00000000",
            "cummulativeQuoteQty": "0.00000000",
            "price": params.get("price", "0"),
            "stopPrice": params.get("stopPrice", "0"),
            "fills": [],
        }
        self.next_id += 1
        if kind == "STOP_LOSS_LIMIT":
            if self.balances.get(base, 0.0) < qty:
                return _error(400, -2010, "Account has insufficient balance for requested action.")
            self.balances[base] -= qty
            self.locked[base] = self.locked.get(base, 0.0) + qty
            self.orders.append(order)
            return httpx.Response(200, json=order)
        if kind != "MARKET":
            return _error(400, -1116, "Invalid orderType.")
        notional = qty * price
        fee_value = notional * self.fee_rate
        if side == "BUY":
            if self.balances.get(quote, 0.0) < notional:
                return _error(400, -2010, "Account has insufficient balance for requested action.")
            self.balances[quote] -= notional
            self.balances[base] = self.balances.get(base, 0.0) + qty
            commission_asset = self.fee_asset or base
            commission = (
                fee_value / price
                if commission_asset == base
                else fee_value / self._fee_price(commission_asset, quote)
            )
        else:
            if self.balances.get(base, 0.0) < qty:
                return _error(400, -2010, "Account has insufficient balance for requested action.")
            self.balances[base] -= qty
            self.balances[quote] = self.balances.get(quote, 0.0) + notional
            commission_asset = self.fee_asset or quote
            commission = (
                fee_value
                if commission_asset == quote
                else fee_value / self._fee_price(commission_asset, quote)
            )
        self.balances[commission_asset] = self.balances.get(commission_asset, 0.0) - commission
        order.update(
            status="FILLED",
            executedQty=f"{qty:.8f}",
            cummulativeQuoteQty=f"{notional:.8f}",
            fills=[
                {
                    "price": f"{price:.8f}",
                    "qty": f"{qty:.8f}",
                    "commission": f"{commission:.8f}",
                    "commissionAsset": commission_asset,
                    "tradeId": self.next_id,
                }
            ],
        )
        self.orders.append(order)
        return httpx.Response(200, json=order)

    def _fee_price(self, asset: str, quote: str) -> float:
        return self.prices[f"{asset}{quote}"]


def _error(status: int, code: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"code": code, "msg": message})
