from datetime import UTC, datetime, timedelta

import pytest

from tbot.core.models import Fill
from tbot.portfolio.portfolio import Portfolio

T0 = datetime(2024, 1, 1, tzinfo=UTC)


def fill(hour: int, quantity: float, price: float, fee: float) -> Fill:
    return Fill(T0 + timedelta(hours=hour), "BTC", quantity, price, fee)


def test_round_trip_with_adds_and_partial_exits() -> None:
    portfolio = Portfolio(1000.0)
    for f in [fill(0, 1, 100, 0.1), fill(1, 1, 110, 0.11), fill(2, -1.5, 120, 0.18)]:
        portfolio.apply(f)
    assert portfolio.position("BTC") == pytest.approx(0.5)
    assert portfolio.trades == []

    portfolio.apply(fill(3, -0.5, 90, 0.045))
    pnl = -100 - 0.1 - 110 - 0.11 + 180 - 0.18 + 45 - 0.045
    [trade] = portfolio.trades
    assert (trade.entry_time, trade.exit_time, trade.direction) == (T0, fill(3, 0, 0, 0).time, 1)
    assert trade.pnl == pytest.approx(pnl)
    assert trade.fees == pytest.approx(0.435)
    assert trade.cost == pytest.approx(210)
    assert portfolio.cash == pytest.approx(1000 + pnl)
    assert portfolio.positions == {}


def test_flip_splits_fill_at_zero() -> None:
    portfolio = Portfolio(1000.0)
    portfolio.apply(fill(0, 2, 50, 0.1))
    portfolio.apply(fill(1, -3, 60, 0.3))

    [trade] = portfolio.trades
    assert trade.pnl == pytest.approx(-100 - 0.1 + 120 - 0.2)
    assert portfolio.position("BTC") == pytest.approx(-1)
    assert portfolio.cash == pytest.approx(1000 - 100 - 0.1 + 180 - 0.3)


def test_equity_and_exposures() -> None:
    portfolio = Portfolio(1000.0)
    portfolio.apply(fill(0, 2, 100, 0.0))
    assert portfolio.equity({"BTC": 150}) == pytest.approx(1100)
    assert portfolio.exposures({"BTC": 150}) == {"BTC": pytest.approx(300 / 1100)}
