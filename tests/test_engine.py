from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from factories import Scripted, price_bars
from tbot.backtest.engine import BacktestEngine, BacktestResult
from tbot.backtest.metrics import compute_metrics
from tbot.core.timeframe import Timeframe
from tbot.execution.rebalance import RebalanceRules
from tbot.execution.sim_broker import CostModel
from tbot.portfolio.allocation import StrategySlot
from tbot.risk.guard import GuardConfig
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
    protective_stop_pct: float = 0.0,
) -> BacktestResult:
    engine = BacktestEngine(
        [StrategySlot(strategy, timeframe, 1.0)],
        bars,
        start=start,
        initial_cash=1000.0,
        costs=COSTS,
        risk=RiskLimits(),
        rules=RULES,
        protective_stop_pct=protective_stop_pct,
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


def test_every_symbol_fills_when_timeframes_are_mixed() -> None:
    btc = price_bars(T0, H4, [100.0, 101.0, 102.0], [101.0, 102.0, 103.0])
    eth = price_bars(T0, H1, [50.0] * 12, [50.0] * 12)
    engine = BacktestEngine(
        [
            StrategySlot(Scripted(["BTC"], {"script": {at(4): {"BTC": 0.4}}}), H4, 0.5),
            StrategySlot(Scripted(["ETH"], {"script": {at(1): {"ETH": 0.4}}}), H1, 0.5),
        ],
        {("BTC", H4): btc, ("ETH", H1): eth},
        start=T0,
        initial_cash=1000.0,
        costs=COSTS,
        risk=RiskLimits(),
        rules=RULES,
    )
    fills = engine.run().fills
    # Hourly ETH decisions no longer push the BTC order back forever.
    btc_fills = fills.filter(pl.col("symbol") == "BTC").select("time", "price").rows()
    assert btc_fills == [(at(4), pytest.approx(101.0 * 1.001))]
    assert fills.filter(pl.col("symbol") == "ETH").height == 1


def test_kill_switch_flattens_and_stays_out() -> None:
    # Fully long from the 1h close; the 30% fall at the 4h close trips a 15% drawdown limit.
    closes = [100, 100, 100, 70, 80, 90, 100]
    bars = price_bars(T0, H1, opens=[100, *closes[:-1]], closes=closes)
    strategy = Scripted(["BTC"], {"script": {at(1): {"BTC": 1.0}}})
    engine = BacktestEngine(
        [StrategySlot(strategy, H1, 1.0)],
        {("BTC", H1): bars},
        start=T0,
        initial_cash=1000.0,
        costs=COSTS,
        risk=RiskLimits(),
        rules=RULES,
        guard=GuardConfig(daily_loss_limit=0, max_drawdown=0.15, stale_seconds=0),
    )
    result = engine.run()
    assert result.halted.startswith("2024-01-01 04:00: drawdown")
    bought, sold = result.fills["quantity"].to_list()
    assert sold == pytest.approx(-bought)
    assert result.fills["time"][1] == at(4)  # flattened at the next open
    assert result.positions.get("BTC", 0.0) == 0.0  # the rebound is not bought back


def test_daily_loss_limit_blocks_entries_only() -> None:
    closes = [100, 100, 90, 90, 90]
    bars = price_bars(T0, H1, opens=[100, *closes[:-1]], closes=closes)
    script = {at(1): {"BTC": 0.5}, at(3): {"BTC": 1.0}}  # adds after a 5% day loss
    engine = BacktestEngine(
        [StrategySlot(Scripted(["BTC"], {"script": script}), H1, 1.0)],
        {("BTC", H1): bars},
        start=T0,
        initial_cash=1000.0,
        costs=COSTS,
        risk=RiskLimits(),
        rules=RULES,
        guard=GuardConfig(daily_loss_limit=0.03, max_drawdown=0, stale_seconds=0),
    )
    result = engine.run()
    assert result.fills.height == 1  # the add was blocked
    assert result.halted == ""


def test_halt_sells_a_symbol_on_a_slower_stream() -> None:
    # Events come hourly (BTC 1h) while ETH trades on 4h bars: the flatten order must keep
    # its decision time until ETH's next bar, or it would never fill.
    flat = [100.0] * 24
    eth = [100.0, 100.0, 60.0, 60.0, 60.0, 60.0]
    bars = {
        ("BTC", H1): price_bars(T0, H1, flat, flat),
        ("ETH", H4): price_bars(T0, H4, [100.0, *eth[:-1]], eth),
    }
    engine = BacktestEngine(
        [
            StrategySlot(Scripted(["ETH"], {"script": {at(4): {"ETH": 1.0}}}), H4, 0.5),
            StrategySlot(Scripted(["BTC"], {"script": {}}), H1, 0.5),
        ],
        bars,
        start=T0,
        initial_cash=1000.0,
        costs=COSTS,
        risk=RiskLimits(),
        rules=RULES,
        guard=GuardConfig(daily_loss_limit=0, max_drawdown=0.15, stale_seconds=0),
    )
    result = engine.run()
    assert result.halted.startswith("2024-01-01 12:00")
    assert result.positions.get("ETH", 0.0) == 0.0
    assert result.fills.filter(pl.col("quantity") < 0)["time"].to_list() == [at(12)]


def test_volatility_scale_uses_only_closed_bars() -> None:
    from tbot.risk.volatility import volatility_scale

    # Calm for 40 bars, wild afterwards: a scale that saw later bars would be smaller.
    closes = [100.0]
    for i in range(1, 60):
        step = 1.01 if i < 41 else 1.10
        closes.append(closes[-1] * (step if i % 2 else 1 / step))
    bars = price_bars(T0, H1, [closes[0], *closes[:-1]], closes)
    engine = BacktestEngine(
        [StrategySlot(Scripted(["BTC"], {"script": {at(40): {"BTC": 1.0}}}), H1, 1.0)],
        {("BTC", H1): bars},
        start=T0,
        initial_cash=1000.0,
        costs=CostModel(fee_rate=0.0, slippage_bps=0.0),
        risk=RiskLimits(target_volatility=0.5, volatility_lookback_days=1),
        rules=RebalanceRules(min_notional=0, rebalance_threshold=0.0),
    )
    first = engine.run().fills.row(0, named=True)
    scale = volatility_scale(closes[:40], H1, 0.5, 1)
    assert scale < 1
    assert first["quantity"] * first["price"] == pytest.approx(scale * 1000.0, rel=1e-6)


def test_a_halt_fills_a_slower_stream_after_the_decision() -> None:
    btc = [100.0] * 12 + [40.0] * 12  # the 12:00 bar crashes: the halt is decided at 13:00
    eth = [100.0] * 6
    engine = BacktestEngine(
        [
            StrategySlot(Scripted(["ETH"], {"script": {at(4): {"ETH": 1.0}}}), H4, 0.5),
            StrategySlot(Scripted(["BTC"], {"script": {at(1): {"BTC": 1.0}}}), H1, 0.5),
        ],
        {
            ("BTC", H1): price_bars(T0, H1, [100.0, *btc[:-1]], btc),
            ("ETH", H4): price_bars(T0, H4, eth, eth),
        },
        start=T0,
        initial_cash=1000.0,
        costs=CostModel(fee_rate=0.0, slippage_bps=0.0),
        risk=RiskLimits(),
        rules=RebalanceRules(min_notional=0, rebalance_threshold=0.0),
        guard=GuardConfig(daily_loss_limit=0, max_drawdown=0.15, stale_seconds=0),
    )
    result = engine.run()
    assert result.halted.startswith("2024-01-01 13:00")
    sells = result.fills.filter(pl.col("symbol") == "ETH", pl.col("quantity") < 0)
    assert sells["time"].to_list() == [at(16)]  # the next 4h open, not the 12:00 one


def test_a_halt_drops_orders_the_strategies_had_queued() -> None:
    btc = [100.0] * 12 + [40.0] * 12  # the halt is decided at 13:00
    eth = [100.0] * 6
    engine = BacktestEngine(
        [
            # Decided at the 12:00 close, it would fill at the 12:00 open, seen at 16:00.
            StrategySlot(Scripted(["ETH"], {"script": {at(12): {"ETH": 1.0}}}), H4, 0.5),
            StrategySlot(Scripted(["BTC"], {"script": {at(1): {"BTC": 1.0}}}), H1, 0.5),
        ],
        {
            ("BTC", H1): price_bars(T0, H1, [100.0, *btc[:-1]], btc),
            ("ETH", H4): price_bars(T0, H4, eth, eth),
        },
        start=T0,
        initial_cash=1000.0,
        costs=CostModel(fee_rate=0.0, slippage_bps=0.0),
        risk=RiskLimits(),
        rules=RebalanceRules(min_notional=0, rebalance_threshold=0.0),
        guard=GuardConfig(daily_loss_limit=0, max_drawdown=0.15, stale_seconds=0),
    )
    result = engine.run()
    assert result.halted.startswith("2024-01-01 13:00")
    assert result.fills.filter(pl.col("symbol") == "ETH").is_empty()  # never bought


def dip_bars(opens: list[float], closes: list[float], low: float) -> pl.DataFrame:
    """Hourly bars whose third bar (2h to 3h) trades down to low."""
    bars = price_bars(T0, H1, opens, closes)
    lows = [low if i == 2 else value for i, value in enumerate(bars["low"])]
    return bars.with_columns(pl.Series("low", lows))


def hold_from_1h() -> Scripted:
    return Scripted(["BTC"], {"script": {at(1): {"BTC": 0.5}}})


def test_a_protective_stop_sells_at_its_level_when_the_low_touches_it() -> None:
    # Bought 5 at the 1h open; the 2h bar dips to 79, under 80% of the 100 close before it.
    bars = dip_bars([100, 100, 100, 95, 95], [100, 100, 95, 95, 95], low=79)
    result = run(hold_from_1h(), {("BTC", H1): bars}, protective_stop_pct=0.2)

    fills = result.fills.select("time", "quantity", "price", "fee").rows()
    assert fills[:2] == [
        (at(1), 5.0, pytest.approx(100.1), pytest.approx(0.5005)),
        (at(3), -5.0, pytest.approx(80 * 0.999), pytest.approx(5 * 80 * 0.999 * 0.001)),
    ]
    # Still long by its signal, the strategy buys back at the next open.
    assert fills[2][0] == at(3)
    assert fills[2][1] > 0
    assert fills[2][2] == pytest.approx(95 * 1.001)
    [trade] = result.trades.rows(named=True)
    assert trade["exit_time"] == at(3)
    assert trade["pnl"] == pytest.approx(5 * 79.92 - 0.3996 - 5 * 100.1 - 0.5005)
    metrics = compute_metrics(result)
    assert metrics.trades == 1
    assert metrics.fees == pytest.approx(sum(row[3] for row in fills))


def test_a_gap_through_the_protective_stop_sells_at_the_open() -> None:
    bars = dip_bars([100, 100, 70, 70, 70], [100, 100, 70, 70, 70], low=70)
    result = run(hold_from_1h(), {("BTC", H1): bars}, protective_stop_pct=0.2)
    sale = result.fills.row(1, named=True)
    assert (sale["time"], sale["quantity"]) == (at(3), -5.0)
    assert sale["price"] == pytest.approx(70 * 0.999)  # under the 80 stop: the open it gapped to


def test_protective_stops_are_off_by_default() -> None:
    bars = {("BTC", H1): dip_bars([100, 100, 100, 95, 95], [100, 100, 95, 95, 95], low=79)}
    engine = BacktestEngine(
        [StrategySlot(hold_from_1h(), H1, 1.0)],
        bars,
        start=T0,
        initial_cash=1000.0,
        costs=COSTS,
        risk=RiskLimits(),
        rules=RULES,
    )
    default = engine.run()
    assert default.fills["time"].to_list() == [at(1)]  # held through the dip
    for pct in (0.0, 0.25):  # off, or a stop the dip never reaches
        other = run(hold_from_1h(), bars, protective_stop_pct=pct)
        assert other.fills.equals(default.fills)
        assert other.equity.equals(default.equity)
