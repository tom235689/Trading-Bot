import math
from datetime import UTC, datetime, timedelta

import pytest

from factories import Scripted, price_bars
from tbot.backtest.engine import BacktestEngine
from tbot.core.config import StrategyConfig, TradingConfig
from tbot.core.timeframe import Timeframe
from tbot.execution.rebalance import RebalanceRules
from tbot.execution.sim_broker import CostModel, SimulatedBroker
from tbot.live.history import BarHistory
from tbot.live.session import TradingSession
from tbot.portfolio.allocation import StrategySlot
from tbot.portfolio.portfolio import Portfolio
from tbot.risk.limits import RiskLimits
from tbot.risk.volatility import lookback_bars, realized_volatility, volatility_scale

D1, H1 = Timeframe.D1, Timeframe.H1
T0 = datetime(2024, 1, 1, tzinfo=UTC)


def test_realized_volatility_hand_calculation() -> None:
    # Log returns +r and -r: mean 0, sample std r * sqrt(2), annualized over 365 days.
    r = math.log(1.1)
    assert realized_volatility([100, 110, 100], D1) == pytest.approx(
        r * math.sqrt(2) * math.sqrt(365)
    )
    assert math.isnan(realized_volatility([100, 110], D1))
    assert math.isnan(realized_volatility([100, 0, 100], D1))


def test_lookback_bars() -> None:
    assert lookback_bars(D1, 30) == 30
    assert lookback_bars(H1, 30) == 720
    assert lookback_bars(Timeframe.H4, 0.1) == 2


def test_volatility_scale_only_scales_down() -> None:
    closes = [100 * 1.1 ** ((-1) ** i) for i in range(40)]  # violent alternation
    realized = realized_volatility(closes[-31:], D1)
    assert volatility_scale(closes, D1, 0.5, 30) == pytest.approx(0.5 / realized)
    assert volatility_scale(closes, D1, 100.0, 30) == 1.0  # calm target above realized
    assert volatility_scale(closes, D1, 0.0, 30) == 1.0  # disabled
    assert volatility_scale(closes[:8], D1, 0.5, 30) == 1.0  # too little history
    assert volatility_scale([100.0] * 40, D1, 0.5, 30) == 1.0  # zero volatility


def test_limits_apply_scales_before_caps() -> None:
    limits = RiskLimits(max_symbol_weight=0.4, max_gross_exposure=0.5)
    # A: 0.5 * 0.4 = 0.2; B clipped to 0.4; gross 0.6 cut to 0.5 scales both by 5/6.
    assert limits.apply({"A": 0.5, "B": 0.5}, {"A": 0.4}) == {
        "A": pytest.approx(1 / 6),
        "B": pytest.approx(1 / 3),
    }
    assert limits.apply({"A": 0.5}) == {"A": 0.4}


CLOSES = [100.0]
for i in range(1, 60):
    CLOSES.append(CLOSES[-1] * (1.08 if i % 2 else 1 / 1.08))
BARS = price_bars(T0, H1, [CLOSES[0], *CLOSES[:-1]], CLOSES)
RISK = RiskLimits(target_volatility=1.0, volatility_lookback_days=1)  # 24 hourly bars
COSTS = CostModel(fee_rate=0.0, slippage_bps=0.0)
RULES = RebalanceRules(min_notional=0, rebalance_threshold=0.0)
SIGNAL_AT = T0 + timedelta(hours=40)


def test_engine_holds_the_scaled_weight() -> None:
    engine = BacktestEngine(
        [StrategySlot(Scripted(["BTC"], {"script": {SIGNAL_AT: {"BTC": 1.0}}}), H1, 1.0)],
        {("BTC", H1): BARS},
        start=T0,
        initial_cash=1000.0,
        costs=COSTS,
        risk=RISK,
        rules=RULES,
    )
    result = engine.run()
    scale = volatility_scale(CLOSES[:40], H1, 1.0, 1)
    assert 0 < scale < 1
    first = result.fills.row(0, named=True)
    assert first["time"] == SIGNAL_AT
    assert first["quantity"] * first["price"] == pytest.approx(scale * 1000.0, rel=1e-6)


def test_session_matches_engine_with_targeting() -> None:
    script = {SIGNAL_AT: {"BTC": 1.0}, SIGNAL_AT + timedelta(hours=10): {"BTC": 0.0}}
    engine = BacktestEngine(
        [StrategySlot(Scripted(["BTC"], {"script": script}), H1, 1.0)],
        {("BTC", H1): BARS},
        start=T0,
        initial_cash=1000.0,
        costs=COSTS,
        risk=RISK,
        rules=RULES,
    )
    expected = engine.run()

    config = TradingConfig(
        initial_cash=1000.0,
        costs=COSTS,
        risk=RISK,
        rebalance=RULES,
        strategies=[StrategyConfig(name="scripted", symbols=["BTC"], timeframe=H1, allocation=1)],
    )
    portfolio = Portfolio(1000.0)
    session = TradingSession(
        config,
        [StrategySlot(Scripted(["BTC"], {"script": script}), H1, 1.0)],
        {("BTC", H1): BarHistory(H1)},
        portfolio,
    )
    broker = SimulatedBroker(COSTS)
    equity = []
    for i in range(len(CLOSES)):
        now = T0 + timedelta(hours=i + 1)
        orders = session.ingest({("BTC", H1): BARS.slice(i, 1)}, now)
        equity.append(session.snapshot(now).equity)
        if i + 1 < len(CLOSES):
            for symbol, quantity in orders.items():
                fill = broker.fill(
                    now,
                    symbol,
                    quantity,
                    float(BARS["open"][i + 1]),
                    portfolio.cash,
                    portfolio.position(symbol),
                )
                if fill is not None:
                    portfolio.apply(fill)
    assert equity == pytest.approx(expected.equity["equity"].to_list())
    assert [f.quantity for f in portfolio.fills] == pytest.approx(
        expected.fills["quantity"].to_list()
    )
