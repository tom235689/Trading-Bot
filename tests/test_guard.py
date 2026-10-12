from datetime import UTC, datetime, timedelta

import pytest

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
    # A new day resets the reference, after its first event judged the day that ended.
    next_day = T0 + timedelta(days=1)
    assert guard.check(next_day, 965.0, next_day).mode == Mode.REDUCE_ONLY
    assert guard.state.day_open_equity == 965.0
    later = next_day + timedelta(hours=4)
    assert guard.check(later, 965.0, later).mode == Mode.NORMAL


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
    guard.shift(-300.0, 1000.0)  # a withdrawal is not a 30% loss
    assert guard.check(now, 700.0, now).mode == Mode.NORMAL
    assert (guard.state.peak_equity, guard.state.day_open_equity) == (700.0, 700.0)


def test_a_transfer_in_a_drawdown_keeps_the_drawdown() -> None:
    guard = RiskGuard(GuardConfig(daily_loss_limit=0, max_drawdown=0.45, stale_seconds=0))
    now = datetime(2024, 1, 1, 12, tzinfo=UTC)
    guard.check(now, 1500.0, now)
    assert guard.check(now, 999.45, now).mode == Mode.NORMAL  # 33% down
    guard.shift(-400.0, 999.45)  # the owner takes 400 out
    assert guard.check(now, 599.45, now).mode == Mode.NORMAL  # still 33% down, not 45.5%
    assert guard.state.peak_equity == pytest.approx(1500.0 * 599.45 / 999.45)


def test_a_book_emptied_by_a_transfer_checks_without_dividing_by_zero() -> None:
    now = datetime(2024, 1, 1, 8, tzinfo=UTC)
    guard = RiskGuard(GuardConfig(), GuardState(day=now.date()))  # peak and day open 0
    assert guard.check(now, -100.0, now).mode == Mode.NORMAL
    guard.resume()
    assert guard.check(now, -100.0, now).mode == Mode.NORMAL


def test_the_bar_that_closes_at_midnight_counts_for_the_day_it_ends() -> None:
    day = datetime(2024, 1, 1, tzinfo=UTC)
    guard = RiskGuard(GuardConfig(daily_loss_limit=0.03, max_drawdown=0.5))
    assert guard.check(day, 1000.0, day).mode == Mode.NORMAL
    nextday = day + timedelta(days=1)  # a 1d bar closes: down 10% over its day
    decision = guard.check(nextday, 900.0, nextday)
    assert (decision.mode, decision.reason) == (Mode.REDUCE_ONLY, "day loss -10.0% beyond 3%")
    later = nextday + timedelta(hours=4)  # the new day opened at 900
    assert guard.check(later, 900.0, later).mode == Mode.NORMAL
    gone = later + timedelta(days=3)  # days without events: no day before to judge
    assert guard.check(gone, 800.0, gone).mode == Mode.NORMAL
