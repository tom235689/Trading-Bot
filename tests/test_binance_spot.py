import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from binance_spot_fake import DEFAULT_RULES, FakeSpot, symbol_info
from tbot.exchange.binance import (
    TESTNET_URL,
    BinanceError,
    BinanceSpot,
    aggregate_fill,
    parse_order,
    parse_rules,
)

T0 = datetime(2024, 1, 1, tzinfo=UTC)


def run[T](
    fake: FakeSpot,
    action: Callable[[BinanceSpot], Awaitable[T]],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> T:
    async def go() -> T:
        async with fake.client() as client:
            spot = BinanceSpot(client, fake.api_key, fake.secret, base_url=TESTNET_URL, clock=clock)
            await spot.load_rules(["BTCUSDT", "ETHUSDT"])
            return await action(spot)

    return asyncio.run(go())


def test_rules_rounding_and_limits() -> None:
    rules = parse_rules(symbol_info("BTCUSDT", DEFAULT_RULES["BTCUSDT"]))
    assert (rules.base, rules.quote) == ("BTC", "USDT")
    assert rules.step_size == Decimal("0.00001")
    assert rules.round_quantity(0.123456789) == pytest.approx(0.12345)
    assert rules.round_quantity(-0.123456789) == pytest.approx(-0.12345)
    assert rules.round_quantity(0.000001) == 0.0
    assert rules.round_price(42000.129) == 42000.12
    assert rules.format_quantity(-0.5) == "0.50000"
    assert rules.format_price(42000.1) == "42000.10"
    assert rules.acceptable(0.0001, 40000) is False  # 4 USDT below the 5 minimum
    assert rules.acceptable(0.0002, 40000) is True


def test_market_order_is_signed_and_filled() -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={"BTCUSDT": 50000.0})
    order = run(fake, lambda spot: spot.market_order("BTCUSDT", 0.01, "tb-1"), clock=lambda: T0)

    assert order.status == "FILLED"
    assert order.client_order_id == "tb-1"
    assert order.executed_qty == pytest.approx(0.01)
    assert order.fills[0].commission_asset == "BTC"
    request = fake.requests[-1]
    assert request.method == "POST"
    assert request.headers["X-MBX-APIKEY"] == "key"
    assert b"timestamp=1704067200000" in request.url.query
    assert b"signature=" in request.url.query
    fill, commissions = aggregate_fill(order, T0)
    assert fill is not None
    assert fill.quantity == pytest.approx(0.01)
    assert fill.price == pytest.approx(50000 * 1.0001)
    assert commissions == {"BTC": pytest.approx(0.01 * 0.001)}


def test_bad_secret_is_rejected() -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={"BTCUSDT": 50000.0}, secret="right")

    async def go() -> None:
        async with fake.client() as client:
            spot = BinanceSpot(client, "key", "wrong", base_url=TESTNET_URL)
            await spot.balances()

    with pytest.raises(BinanceError, match="-1022"):
        asyncio.run(go())


def test_balances_orders_and_cancel() -> None:
    fake = FakeSpot(balances={"USDT": 100.0, "BTC": 0.5}, prices={"BTCUSDT": 50000.0})

    async def go(spot: BinanceSpot) -> tuple[dict[str, float], list[str], int, int]:
        before = {a: b.total for a, b in (await spot.balances()).items()}
        stop = await spot.stop_loss_order("BTCUSDT", 0.2, 45000.0, 44900.0, "stop-1")
        assert stop.status == "NEW"
        assert stop.stop_price == 45000.0
        open_ids = [o.client_order_id for o in await spot.open_orders("BTCUSDT")]
        assert (await spot.get_order("BTCUSDT", "stop-1")) is not None
        assert (await spot.get_order("BTCUSDT", "missing")) is None
        canceled = await spot.cancel_order("BTCUSDT", stop.order_id)
        assert canceled.status == "CANCELED"
        first = await spot.cancel_all("BTCUSDT")
        again = await spot.cancel_all("BTCUSDT")
        return before, open_ids, first, again

    before, open_ids, first, again = run(fake, go)
    assert before == {"USDT": 100.0, "BTC": 0.5}
    assert open_ids == ["stop-1"]
    assert (first, again) == (0, 0)


def test_get_retries_but_post_does_not() -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={"BTCUSDT": 50000.0})
    fake.fail_next = [httpx.Response(429, headers={"Retry-After": "0"})]
    assert run(fake, lambda spot: spot.balances())["USDT"].free == 1000.0

    async def post_after_outage(spot: BinanceSpot) -> None:
        fake.fail_next = [httpx.Response(503)]
        await spot.market_order("BTCUSDT", 0.01, "tb-2")

    with pytest.raises(BinanceError) as exc:
        run(fake, post_after_outage)
    assert exc.value.status == 503
    assert fake.order_count("MARKET") == 0


def test_filter_and_balance_errors() -> None:
    fake = FakeSpot(balances={"USDT": 100.0}, prices={"BTCUSDT": 50000.0})
    with pytest.raises(BinanceError, match="-1013"):
        run(fake, lambda spot: spot.market_order("BTCUSDT", 0.00005, "small"))
    with pytest.raises(BinanceError, match="-2010"):
        run(fake, lambda spot: spot.market_order("BTCUSDT", 0.01, "big"))


def test_load_rules_rejects_unknown_symbol() -> None:
    fake = FakeSpot(balances={}, prices={})

    async def go() -> None:
        async with fake.client() as client:
            await BinanceSpot(client, "key", "secret").load_rules(["DOGEUSDT"])

    with pytest.raises(BinanceError, match="unknown symbols"):
        asyncio.run(go())


def test_parse_order_without_fills() -> None:
    data = {
        "symbol": "BTCUSDT",
        "orderId": 7,
        "side": "SELL",
        "type": "MARKET",
        "status": "FILLED",
        "executedQty": "0.5",
        "cummulativeQuoteQty": "25000",
    }
    order = parse_order(data)
    assert order.done
    assert order.stop_price is None
    fill, commissions = aggregate_fill(order, T0)
    assert fill is not None
    assert (fill.quantity, fill.price, commissions) == (-0.5, 50000.0, {})

    unfilled = parse_order(
        {**data, "status": "NEW", "executedQty": "0", "cummulativeQuoteQty": "0"}
    )
    assert aggregate_fill(unfilled, T0) == (None, {})


def test_timestamp_error_resyncs_the_clock_and_signs_again() -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={"BTCUSDT": 50000.0})
    resyncs: list[int] = []

    async def go() -> float:
        async def resync() -> None:
            resyncs.append(1)

        async with fake.client() as client:
            spot = BinanceSpot(
                client, fake.api_key, fake.secret, base_url=TESTNET_URL, resync=resync
            )
            fake.fail_next = [
                httpx.Response(400, json={"code": -1021, "msg": "Timestamp outside recvWindow."})
            ]
            return (await spot.balances())["USDT"].free

    assert asyncio.run(go()) == 1000.0
    assert resyncs == [1]


def test_a_long_ban_fails_at_once_instead_of_stalling_the_bot() -> None:
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={"BTCUSDT": 50000.0})
    fake.fail_next = [httpx.Response(418, headers={"Retry-After": "7200"})]  # an IP ban
    with pytest.raises(BinanceError) as exc:
        run(fake, lambda spot: spot.balances())
    assert exc.value.status == 418
    assert len(fake.requests) == 1
