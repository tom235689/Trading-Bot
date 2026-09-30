from datetime import UTC, datetime, timedelta

import pytest

from factories import Scripted, price_bars
from tbot.backtest.engine import BacktestEngine
from tbot.core.config import StrategyConfig, TradingConfig
from tbot.core.timeframe import Timeframe
from tbot.execution.rebalance import RebalanceRules
from tbot.execution.sim_broker import CostModel, SimulatedBroker
from tbot.live.history import BarHistory
from tbot.live.session import StreamKey, TradingSession, replay_history
from tbot.portfolio.allocation import StrategySlot
from tbot.portfolio.portfolio import Portfolio
from tbot.risk.limits import RiskLimits

H1, H4 = Timeframe.H1, Timeframe.H4
T0 = datetime(2024, 1, 1, tzinfo=UTC)
COSTS = CostModel(fee_rate=0.001, slippage_bps=10)
RULES = RebalanceRules(min_notional=0, rebalance_threshold=0.05)
CONFIG = TradingConfig(
    initial_cash=1000.0,
    costs=COSTS,
    risk=RiskLimits(),
    rebalance=RULES,
    strategies=[
        StrategyConfig(name="scripted", symbols=["BTC", "ETH"], timeframe=H1, allocation=1)
    ],
)
BTC = [100, 102, 101, 105, 110, 108, 112, 115, 111, 109, 113, 118]
ETH = [50, 51, 49, 52, 55, 57, 54, 53, 56, 58, 60, 59]
BARS = {
    ("BTC", H1): price_bars(T0, H1, [BTC[0], *BTC[:-1]], BTC),
    ("ETH", H1): price_bars(T0, H1, [ETH[0], *ETH[:-1]], ETH),
}


def at(hours: int) -> datetime:
    return T0 + timedelta(hours=hours)


SCRIPT = {
    at(3): {"BTC": 0.4, "ETH": 0.3},
    at(6): {"BTC": 0.0, "ETH": 0.6},
    at(9): {"BTC": 0.5, "ETH": 0.0},
}


def test_session_matches_backtest_engine() -> None:
    engine = BacktestEngine(
        [StrategySlot(Scripted(["BTC", "ETH"], {"script": SCRIPT}), H1, 1.0)],
        BARS,
        start=T0,
        initial_cash=1000.0,
        costs=COSTS,
        risk=RiskLimits(),
        rules=RULES,
    )
    expected = engine.run()

    portfolio = Portfolio(1000.0)
    session = TradingSession(
        CONFIG,
        [StrategySlot(Scripted(["BTC", "ETH"], {"script": SCRIPT}), H1, 1.0)],
        {key: BarHistory(H1) for key in BARS},
        portfolio,
    )
    broker = SimulatedBroker(COSTS)
    equity = []
    for i in range(len(BTC)):
        now = at(i + 1)
        orders = session.ingest({key: bars.slice(i, 1) for key, bars in BARS.items()}, now)
        equity.append(session.snapshot(now).equity)
        if i + 1 == len(BTC):
            break
        # Like the backtest: fill at the next bar's open, sells first.
        for symbol, quantity in sorted(orders.items(), key=lambda item: item[1]):
            reference = float(BARS[(symbol, H1)]["open"][i + 1])
            fill = broker.fill(
                at(i + 1), symbol, quantity, reference, portfolio.cash, portfolio.position(symbol)
            )
            if fill is not None:
                portfolio.apply(fill)

    assert len(portfolio.fills) >= 4
    assert equity == pytest.approx(expected.equity["equity"].to_list())
    fills = portfolio.fills
    assert [(f.time, f.symbol) for f in fills] == expected.fills.select("time", "symbol").rows()
    for column, values in (
        ("quantity", [f.quantity for f in fills]),
        ("price", [f.price for f in fills]),
        ("fee", [f.fee for f in fills]),
    ):
        assert values == pytest.approx(expected.fills[column].to_list())


def test_replay_feeds_bars_in_close_order_without_trading() -> None:
    strategy = Scripted(["BTC", "ETH"], {"script": SCRIPT})
    histories: dict[StreamKey, BarHistory] = {key: BarHistory(H1) for key in BARS}
    session = TradingSession(
        CONFIG, [StrategySlot(strategy, H1, 1.0)], histories, Portfolio(1000.0)
    )

    replay_history(session, BARS)

    assert [ctx.time for ctx in strategy.calls] == [at(i + 1) for i in range(len(BTC))]
    assert all(len(ctx.bars("BTC")) == len(ctx.bars("ETH")) for ctx in strategy.calls)
    assert session.marks == {"BTC": 118.0, "ETH": 59.0}
    assert session.portfolio.fills == []
    assert session.slots[0].targets == {"BTC": 0.5, "ETH": 0.0}


def test_session_requires_history_for_each_symbol() -> None:
    with pytest.raises(ValueError, match="no history for ETH"):
        TradingSession(
            CONFIG,
            [StrategySlot(Scripted(["BTC", "ETH"]), H1, 1.0)],
            {("BTC", H1): BarHistory(H1)},
            Portfolio(1000.0),
        )


def test_session_orders_a_symbol_only_when_its_own_stream_closes() -> None:
    config = TradingConfig(
        initial_cash=1000.0,
        costs=COSTS,
        risk=RiskLimits(),
        rebalance=RebalanceRules(min_notional=0, rebalance_threshold=0),
        strategies=[
            StrategyConfig(name="scripted", symbols=["BTC"], timeframe=H4, allocation=0.5),
            StrategyConfig(name="scripted", symbols=["ETH"], timeframe=H1, allocation=0.5),
        ],
    )
    btc_slot = StrategySlot(Scripted(["BTC"], {"script": {at(4): {"BTC": 0.4}}}), H4, 0.5)
    session = TradingSession(
        config,
        [btc_slot, StrategySlot(Scripted(["ETH"]), H1, 0.5)],
        {("BTC", H4): BarHistory(H4), ("ETH", H1): BarHistory(H1)},
        Portfolio(1000.0),
    )
    btc = price_bars(T0, H4, [100.0], [100.0])
    eth = price_bars(T0, H1, [50.0] * 4, [50.0] * 4)
    btc_slot.targets = {"BTC": 0.4}  # e.g. restored after a restart
    session.marks["BTC"] = 100.0
    for i in range(3):  # only ETH closes: BTC waits for its own bar, as in the backtest
        orders = session.ingest({("ETH", H1): eth.slice(i, 1)}, at(i + 1))
        assert "BTC" not in orders
    both = {("ETH", H1): eth.slice(3, 1), ("BTC", H4): btc}
    assert "BTC" in session.ingest(both, at(4))
