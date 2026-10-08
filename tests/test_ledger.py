from datetime import UTC, datetime, timedelta
from pathlib import Path

from tbot.core.models import Fill
from tbot.live.backup import backup_ledger, daily_backups
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


def test_old_ledgers_gain_client_ids_and_orders_in_doubt_are_listed(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, time TEXT NOT NULL, symbol TEXT NOT NULL, "
        "quantity REAL NOT NULL, status TEXT NOT NULL, note TEXT NOT NULL DEFAULT '')"
    )
    conn.commit()
    conn.close()
    with Ledger(path) as ledger:
        ledger.add_order(T0, "BTCUSDT", 1.0, "pending", "", "a")
        ledger.add_order(T0, "BTCUSDT", 1.0, "filled", "", "a")
        ledger.add_order(T0, "ETHUSDT", 2.0, "pending", "", "b")
        ledger.add_order(T0, "ETHUSDT", 2.0, "unknown", "503", "b")
        assert [r.client_id for r in ledger.unresolved_orders()] == ["b"]
        assert ledger.client_id_used("a")
        assert not ledger.client_id_used("c")


def test_backups_copy_the_ledger_and_keep_the_newest_days(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite"
    with Ledger(path) as ledger:
        ledger.add_event(T0, "info", "started")
        for day in range(4):
            target = backup_ledger(ledger, path, T0 + timedelta(days=day), keep=2)
        ledger.add_event(T0, "info", "after the backup")
    assert target == tmp_path / "backups" / "paper-20240104.sqlite"
    kept = [p.name for p in daily_backups(path)]
    assert kept == ["paper-20240103.sqlite", "paper-20240104.sqlite"]
    assert not list((tmp_path / "backups").glob("*.partial"))
    with Ledger(target) as copy:
        assert [e.message for e in copy.recent_events(5)] == ["started"]
