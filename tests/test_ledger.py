from datetime import UTC, datetime, timedelta
from pathlib import Path

from tbot.core.models import Fill
from tbot.live.ledger import EquityPoint, Event, Ledger, config_hash

T0 = datetime(2024, 1, 1, tzinfo=UTC)


def test_round_trip_and_persistence(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    ledger = Ledger(path)
    fill = Fill(T0, "BTCUSDT", 1.5, 100.0, 0.1)
    ledger.add_fill(fill, reference_price=99.9)
    ledger.add_order(T0, "BTCUSDT", 1.5, "filled")
    ledger.add_signals(T0, "donchian_trend", {"BTCUSDT": 0.5, "ETHUSDT": 0.0})
    ledger.add_equity(EquityPoint(T0, 1000.0, 500.0, 0.5))
    ledger.add_equity(EquityPoint(T0 + timedelta(hours=1), 1100.0, 500.0, 0.55))
    ledger.add_event(T0, "info", "started")
    assert ledger.get_meta("config_hash") is None
    ledger.set_meta("config_hash", "abc")
    ledger.close()

    reopened = Ledger(path)
    assert reopened.fills() == [fill]
    assert reopened.recent_fills(1) == [fill]
    latest = reopened.latest_equity()
    assert latest is not None
    assert (latest.equity, latest.exposure) == (1100.0, 0.55)
    before = reopened.equity_before(T0 + timedelta(minutes=30))
    assert before is not None
    assert before.equity == 1000.0
    assert reopened.equity_before(T0 - timedelta(seconds=1)) is None
    assert reopened.recent_events(5) == [Event(T0, "info", "started")]
    assert reopened.get_meta("config_hash") == "abc"
    assert reopened.counts() == {"fills": 1, "orders": 1, "signals": 2, "equity": 2, "events": 1}
    reopened.close()


def test_empty_ledger(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "empty.sqlite")
    assert ledger.fills() == []
    assert ledger.latest_equity() is None
    assert ledger.recent_events(3) == []
    ledger.close()


def test_config_hash_is_stable() -> None:
    assert config_hash('{"a": 1}') == config_hash('{"a": 1}')
    assert config_hash('{"a": 1}') != config_hash('{"a": 2}')
    assert len(config_hash("x")) == 16
