from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from factories import Scripted, price_bars
from tbot.backtest.engine import BacktestEngine, BacktestResult, StrategySlot
from tbot.core.timeframe import Timeframe
from tbot.execution.rebalance import RebalanceRules
from tbot.execution.sim_broker import CostModel
from tbot.risk.limits import RiskLimits
from tbot.strategies.base import Strategy

H1, H4 = Timeframe.H1, Timeframe.H4
T0 = datetime(2024, 1, 1, tzinfo=UTC)
COSTS = CostModel(fee_rate=0.001, slippage_bps=10)
RULES = RebalanceRules(min_notional=0, rebalance_threshold=0.1)


def at(hours: int) -> datetime:
    return T0 + timedelta(hours=hours)


def run(
    strategy: Strategy,
    bars: dict[tuple[str, Timeframe], pl.DataFrame],
    timeframe: Timeframe = H1,
    start: datetime = T0,
) -> BacktestResult:
    engine = BacktestEngine(
        [StrategySlot(strategy, timeframe, 1.0)],
        bars,
        start=start,
        initial_cash=1000.0,
        costs=COSTS,
        risk=RiskLimits(),
        rules=RULES,
    )
    return engine.run()


def test_matches_hand_calculation() -> None:
    # Bar opens at 0h..4h. Buy half at the 1h close, sell at the 3h close.
    bars = price_bars(T0, H1, opens=[100, 100, 110, 120, 100], closes=[100, 110, 120, 100, 100])
    strategy = Scripted(["BTC"], {"script": {at(1): {"BTC": 0.5}, at(3): {"BTC": 0.0}}})
    result = run(strategy, {("BTC", H1): bars})

    # Buy 5 = 0.5 * 1000 / 100 at the 1h open plus 10 bps: 100.1, fee 0.5005.
    # The 2h drift (500 of 1050 -> 550) is below the 10% threshold, so no trade.
    # Sell 5 at the 3h open minus 10 bps: 119.88, fee 0.5994.
    buy_cash = 1000 - 5 * 100.1 - 0.5005
    final_cash = buy_cash + 5 * 119.88 - 0.5994
    assert result.fills.select("time", "quantity", "price", "fee").rows() == [
        (at(1), 5.0, pytest.approx(100.1), pytest.approx(0.5005)),
        (at(3), -5.0, pytest.approx(119.88), pytest.approx(0.5994)),
    ]
    assert result.equity["time"].to_list() == [at(h) for h in range(1, 6)]
    assert result.equity["equity"].to_list() == pytest.approx(
        [1000, buy_cash + 5 * 110, buy_cash + 5 * 120, final_cash, final_cash]
    )
    assert result.equity["exposure"][1] == pytest.approx(550 / (buy_cash + 550))

    trade = result.trades.row(0, named=True)
    pnl = final_cash - 1000
    assert (trade["entry_time"], trade["exit_time"]) == (at(1), at(3))
    assert trade["pnl"] == pytest.approx(pnl)
    assert trade["fees"] == pytest.approx(0.5005 + 0.5994)
    assert trade["return"] == pytest.approx(pnl / 500.5)
    assert result.positions == {}


def test_strategy_sees_only_closed_bars() -> None:
    strategy = Scripted(["BTC"])
    run(strategy, {("BTC", H1): price_bars(T0, H1, [100] * 4, [100] * 4)})
    for count, ctx in enumerate(strategy.calls, start=1):
        window = ctx.bars("BTC")
        assert len(window) == count
        assert window.open_time[-1].item() + timedelta(hours=1) == ctx.time.replace(tzinfo=None)
        with pytest.raises(ValueError, match="read-only"):
            window.close[0] = 0.0


def test_nothing_trades_before_start() -> None:
    bars = price_bars(T0, H1, [100] * 6, [100] * 6)
    strategy = Scripted(["BTC"], {"script": {at(1): {"BTC": 0.5}}})
    result = run(strategy, {("BTC", H1): bars}, start=at(3))

    assert len(strategy.calls) == 6  # still called during warmup
    assert result.equity["time"][0] == at(3)
    assert result.fills["time"].to_list() == [at(3)]  # decided at 3h, filled at the 3h open


def test_coarse_signal_fills_on_finest_stream() -> None:
    opens = [100 + i for i in range(8)]
    bars = {
        ("BTC", H1): price_bars(T0, H1, opens, opens),
        ("BTC", H4): price_bars(T0, H4, [100, 104], [104, 108]),
    }
    strategy = Scripted(["BTC"], {"script": {at(4): {"BTC": 0.5}}})
    result = run(strategy, bars, timeframe=H4)

    assert result.fills.select("time", "price").rows() == [(at(4), pytest.approx(104 * 1.001))]
    assert len(result.equity) == 8  # marked every hour


def test_order_waits_for_next_available_bar() -> None:
    bars = price_bars(T0, H1, [100, 101, 103], [100, 101, 103])
    bars = bars.with_columns(pl.Series("open_time", [at(0), at(1), at(3)]).dt.cast_time_unit("ms"))
    strategy = Scripted(["BTC"], {"script": {at(2): {"BTC": 0.5}}})
    result = run(strategy, {("BTC", H1): bars})
    assert result.fills.select("time", "price").rows() == [(at(3), pytest.approx(103 * 1.001))]


@pytest.mark.parametrize(
    ("targets", "message"), [({"BTC": 1.5}, "outside"), ({"ETH": 0.5}, "unsubscribed")]
)
def test_invalid_targets_raise(targets: dict[str, float], message: str) -> None:
    strategy = Scripted(["BTC"], {"script": {at(1): targets}})
    with pytest.raises(ValueError, match=message):
        run(strategy, {("BTC", H1): price_bars(T0, H1, [100] * 2, [100] * 2)})


def test_missing_stream_raises() -> None:
    with pytest.raises(ValueError, match="no bars loaded for ETH"):
        run(Scripted(["ETH"]), {("BTC", H1): price_bars(T0, H1, [100], [100])})
