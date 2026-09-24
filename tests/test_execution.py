from datetime import UTC, datetime

import pytest

from tbot.execution.rebalance import RebalanceRules, plan_orders
from tbot.execution.sim_broker import CostModel, SimulatedBroker
from tbot.risk.limits import RiskLimits

T0 = datetime(2024, 1, 1, tzinfo=UTC)
RULES = RebalanceRules(min_notional=10, rebalance_threshold=0.02)
PRICES = {"A": 100.0, "B": 50.0}


def test_plan_buys_toward_target() -> None:
    assert plan_orders({"A": 0.5}, {}, PRICES, 1000, RULES) == {"A": pytest.approx(5)}


def test_plan_closes_exact_position() -> None:
    assert plan_orders({"A": 0.0}, {"A": 3.3}, PRICES, 1000, RULES) == {"A": -3.3}
    assert plan_orders({}, {"B": 0.7}, PRICES, 1000, RULES) == {"B": -0.7}


def test_plan_skips_small_changes() -> None:
    # 0.51 -> 0.5 of 1000 is a 10 notional change, under the 2% threshold.
    assert plan_orders({"A": 0.5}, {"A": 5.1}, PRICES, 1000, RULES) == {}
    # Under the minimum notional even when closing.
    assert plan_orders({"A": 0.0}, {"A": 0.05}, PRICES, 1000, RULES) == {}


def test_plan_skips_unpriced_symbols() -> None:
    assert plan_orders({"C": 0.5}, {}, PRICES, 1000, RULES) == {}


COSTS = CostModel(fee_rate=0.001, slippage_bps=10)


def test_broker_applies_slippage_and_fee() -> None:
    broker = SimulatedBroker(COSTS)
    buy = broker.fill(T0, "A", 2, 100, cash=1000, position=0)
    sell = broker.fill(T0, "A", -2, 100, cash=0, position=2)
    assert buy is not None
    assert sell is not None
    assert buy.price == pytest.approx(100.1)
    assert buy.fee == pytest.approx(0.2002)
    assert sell.price == pytest.approx(99.9)
    assert sell.fee == pytest.approx(0.1998)


def test_broker_caps_buys_by_cash_and_sells_by_position() -> None:
    broker = SimulatedBroker(COSTS)
    buy = broker.fill(T0, "A", 20, 100, cash=1000, position=0)
    sell = broker.fill(T0, "A", -5, 100, cash=0, position=2)
    assert buy is not None
    assert sell is not None
    assert buy.quantity * buy.price + buy.fee == pytest.approx(1000)
    assert sell.quantity == -2
    assert broker.fill(T0, "A", 1, 100, cash=0, position=0) is None
    assert broker.fill(T0, "A", -1, 100, cash=0, position=0) is None


def test_risk_limits() -> None:
    limits = RiskLimits(max_symbol_weight=0.4, max_gross_exposure=0.6)
    assert limits.apply({"A": 0.5, "B": -0.2}) == {"A": pytest.approx(0.4), "B": 0.0}
    assert limits.apply({"A": 0.4, "B": 0.4}) == {
        "A": pytest.approx(0.3),
        "B": pytest.approx(0.3),
    }
    short = RiskLimits(long_only=False, max_symbol_weight=0.5)
    assert short.apply({"A": -0.9}) == {"A": -0.5}
