from datetime import UTC, datetime, timedelta

from tbot.risk.guard import GuardConfig, GuardState, Mode, RiskGuard

T0 = datetime(2024, 1, 1, 8, tzinfo=UTC)
CONFIG = GuardConfig(daily_loss_limit=0.03, max_drawdown=0.15, stale_seconds=900)


def test_normal_then_reduce_only_on_daily_loss() -> None:
    guard = RiskGuard(CONFIG)
    assert guard.check(T0, 1000.0, T0).mode == Mode.NORMAL
    assert guard.state.day_open_equity == 1000.0
    assert guard.check(T0 + timedelta(hours=4), 975.0, T0 + timedelta(hours=4)).mode == Mode.NORMAL
    decision = guard.check(T0 + timedelta(hours=8), 965.0, T0 + timedelta(hours=8))
    assert decision.mode == Mode.REDUCE_ONLY
    assert "day loss -3.5%" in decision.reason
    # A new day resets the reference.
    next_day = T0 + timedelta(days=1)
    assert guard.check(next_day, 965.0, next_day).mode == Mode.NORMAL
    assert guard.state.day_open_equity == 965.0


def test_drawdown_halts_and_persists_until_resumed() -> None:
    guard = RiskGuard(CONFIG)
    guard.check(T0, 1000.0, T0)
    decision = guard.check(T0 + timedelta(hours=4), 840.0, T0 + timedelta(hours=4))
    assert decision.mode == Mode.HALT
    assert "drawdown -16.0%" in decision.reason

    restored = RiskGuard(CONFIG, GuardState.model_validate(guard.state.model_dump()))
    assert restored.check(T0 + timedelta(days=2), 2000.0, T0 + timedelta(days=2)).mode == Mode.HALT
    restored.resume()
    assert (
        restored.check(T0 + timedelta(days=2), 2000.0, T0 + timedelta(days=2)).mode == Mode.NORMAL
    )
    assert restored.state.peak_equity == 2000.0


def test_stale_bar_blocks_the_event() -> None:
    guard = RiskGuard(CONFIG)
    decision = guard.check(T0, 1000.0, T0 - timedelta(seconds=1000))
    assert decision.mode == Mode.BLOCK
    assert "1000s old" in decision.reason


def test_disabled_rules() -> None:
    guard = RiskGuard(GuardConfig(daily_loss_limit=0, max_drawdown=0, stale_seconds=0))
    guard.check(T0, 1000.0, T0)
    assert guard.check(T0, 1.0, T0 - timedelta(days=9)).mode == Mode.NORMAL


def test_filter_orders_by_mode() -> None:
    orders = {"BTC": 1.0, "ETH": -0.5, "SOL": -2.0, "XRP": 3.0}
    positions = {"BTC": 2.0, "ETH": 1.0, "SOL": 1.0, "XRP": -1.0}
    assert RiskGuard.filter_orders(orders, positions, Mode.NORMAL) == orders
    assert RiskGuard.filter_orders(orders, positions, Mode.BLOCK) == {}
    assert RiskGuard.filter_orders(orders, positions, Mode.HALT) == {}
    # Reduce only: BTC adds to a long (dropped), ETH trims a long, SOL is clipped to the
    # position, XRP covers part of a short.
    assert RiskGuard.filter_orders(orders, positions, Mode.REDUCE_ONLY) == {
        "ETH": -0.5,
        "SOL": -1.0,
        "XRP": 1.0,
    }
    assert RiskGuard.flatten_orders({"BTC": 2.0, "ETH": 0.0, "XRP": -1.0}) == {
        "BTC": -2.0,
        "XRP": 1.0,
    }


def test_money_moved_from_outside_shifts_the_levels() -> None:
    guard = RiskGuard(GuardConfig(daily_loss_limit=0.03, max_drawdown=0.15, stale_seconds=0))
    now = datetime(2024, 1, 1, 12, tzinfo=UTC)
    assert guard.check(now, 1000.0, now).mode == Mode.NORMAL
    guard.shift(-300.0)  # a withdrawal is not a 30% loss
    assert guard.check(now, 700.0, now).mode == Mode.NORMAL
    assert (guard.state.peak_equity, guard.state.day_open_equity) == (700.0, 700.0)
