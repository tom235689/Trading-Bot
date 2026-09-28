"""SQLite ledger: the durable record of a paper or live session."""

import hashlib
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tbot.core.models import Fill

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY, time TEXT NOT NULL, symbol TEXT NOT NULL, quantity REAL NOT NULL,
    price REAL NOT NULL, fee REAL NOT NULL, reference_price REAL NOT NULL);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY, time TEXT NOT NULL, symbol TEXT NOT NULL, quantity REAL NOT NULL,
    status TEXT NOT NULL, note TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY, time TEXT NOT NULL, strategy TEXT NOT NULL, symbol TEXT NOT NULL,
    target REAL NOT NULL);
CREATE TABLE IF NOT EXISTS equity (
    id INTEGER PRIMARY KEY, time TEXT NOT NULL, equity REAL NOT NULL, cash REAL NOT NULL,
    exposure REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, time TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS equity_time ON equity (time);
"""


@dataclass(frozen=True)
class EquityPoint:
    time: datetime
    equity: float
    cash: float
    exposure: float


@dataclass(frozen=True)
class Event:
    time: datetime
    level: str
    message: str


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text)


class Ledger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # meta

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    # fills and orders

    def add_fill(self, fill: Fill, reference_price: float) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO fills (time, symbol, quantity, price, fee, reference_price) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    _iso(fill.time),
                    fill.symbol,
                    fill.quantity,
                    fill.price,
                    fill.fee,
                    reference_price,
                ),
            )

    def fills(self) -> list[Fill]:
        rows = self.conn.execute(
            "SELECT time, symbol, quantity, price, fee FROM fills ORDER BY id"
        ).fetchall()
        return [Fill(_parse(t), s, q, p, f) for t, s, q, p, f in rows]

    def recent_fills(self, limit: int) -> list[Fill]:
        return self.fills()[-limit:]

    def add_order(
        self, time: datetime, symbol: str, quantity: float, status: str, note: str = ""
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO orders (time, symbol, quantity, status, note) VALUES (?, ?, ?, ?, ?)",
                (_iso(time), symbol, quantity, status, note),
            )

    # signals and equity

    def add_signals(self, time: datetime, strategy: str, targets: Mapping[str, float]) -> None:
        rows = [(_iso(time), strategy, symbol, target) for symbol, target in targets.items()]
        with self.conn:
            self.conn.executemany(
                "INSERT INTO signals (time, strategy, symbol, target) VALUES (?, ?, ?, ?)", rows
            )

    def add_equity(self, point: EquityPoint) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO equity (time, equity, cash, exposure) VALUES (?, ?, ?, ?)",
                (_iso(point.time), point.equity, point.cash, point.exposure),
            )

    def latest_equity(self) -> EquityPoint | None:
        row = self.conn.execute(
            "SELECT time, equity, cash, exposure FROM equity ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return None if row is None else EquityPoint(_parse(row[0]), row[1], row[2], row[3])

    def equity_before(self, time: datetime) -> EquityPoint | None:
        """Latest snapshot taken at or before time."""
        row = self.conn.execute(
            "SELECT time, equity, cash, exposure FROM equity WHERE time <= ? "
            "ORDER BY time DESC LIMIT 1",
            (_iso(time),),
        ).fetchone()
        return None if row is None else EquityPoint(_parse(row[0]), row[1], row[2], row[3])

    # events

    def add_event(self, time: datetime, level: str, message: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO events (time, level, message) VALUES (?, ?, ?)",
                (_iso(time), level, message),
            )

    def recent_events(self, limit: int) -> list[Event]:
        rows = self.conn.execute(
            "SELECT time, level, message FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Event(_parse(t), lv, m) for t, lv, m in reversed(rows)]

    def counts(self) -> dict[str, int]:
        tables = ("fills", "orders", "signals", "equity", "events")
        return {
            t: int(self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t in tables
        }


def config_hash(config_json: str) -> str:
    return hashlib.sha256(config_json.encode()).hexdigest()[:16]
