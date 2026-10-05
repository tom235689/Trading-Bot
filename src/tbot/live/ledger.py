"""SQLite ledger: the durable record of a paper or live session."""

import hashlib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from tbot.core.models import Fill

try:
    import sqlite3
except ImportError as exc:  # e.g. Windows Smart App Control blocking _sqlite3.pyd
    SQLITE_ERROR: ImportError | None = exc
else:
    SQLITE_ERROR = None


class LedgerUnavailable(RuntimeError):
    """The SQLite driver cannot be loaded, so no session can keep a ledger."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY, time TEXT NOT NULL, symbol TEXT NOT NULL, quantity REAL NOT NULL,
    price REAL NOT NULL, fee REAL NOT NULL, reference_price REAL NOT NULL);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY, time TEXT NOT NULL, symbol TEXT NOT NULL, quantity REAL NOT NULL,
    status TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', client_id TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY, time TEXT NOT NULL, strategy TEXT NOT NULL, symbol TEXT NOT NULL,
    target REAL NOT NULL);
CREATE TABLE IF NOT EXISTS equity (
    id INTEGER PRIMARY KEY, time TEXT NOT NULL, equity REAL NOT NULL, cash REAL NOT NULL,
    exposure REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, time TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS adjustments (
    id INTEGER PRIMARY KEY, time TEXT NOT NULL, symbol TEXT NOT NULL, quantity REAL NOT NULL,
    cash REAL NOT NULL, note TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS equity_time ON equity (time);
"""
# Order rows that end an order's story; "pending" and "unknown" still need an answer.
RESOLVED = ("filled", "unfilled", "failed")


@dataclass(frozen=True)
class OrderRecord:
    time: datetime
    symbol: str
    quantity: float
    status: str
    client_id: str


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


@dataclass(frozen=True)
class Adjustment:
    """Position and cash delta from reconciling against the exchange. Symbol "" means cash only."""

    time: datetime
    symbol: str
    quantity: float
    cash: float
    note: str = ""


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text)


class Ledger:
    def __init__(self, path: Path) -> None:
        if SQLITE_ERROR is not None:
            raise LedgerUnavailable(
                f"cannot load SQLite ({SQLITE_ERROR}). On Windows, Smart App Control can "
                "block unsigned Python modules: install Python from python.org and run "
                "`uv venv --python <path to its python.exe>` then `uv sync`, as the "
                "README explains."
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self._atomic = False
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")  # every committed row survives power loss
        self.conn.executescript(SCHEMA)
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(orders)")}
        if "client_id" not in columns:  # ledgers from before client ids were stored
            with self.conn:
                self.conn.execute(
                    "ALTER TABLE orders ADD COLUMN client_id TEXT NOT NULL DEFAULT ''"
                )
        self.conn.execute("CREATE INDEX IF NOT EXISTS orders_client ON orders (client_id)")

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """Writes inside commit together or not at all, so a crash cannot split them."""
        if self._atomic:
            yield
            return
        with self.conn:
            self._atomic = True
            try:
                yield
            finally:
                self._atomic = False

    @contextmanager
    def _write(self) -> Iterator[None]:
        if self._atomic:  # the enclosing atomic() commits
            yield
        else:
            with self.conn:
                yield

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # meta

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def set_meta(self, key: str, value: str) -> None:
        with self._write():
            self.conn.execute("REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def delete_meta(self, key: str) -> None:
        with self._write():
            self.conn.execute("DELETE FROM meta WHERE key = ?", (key,))

    # fills and orders

    def add_fill(self, fill: Fill, reference_price: float) -> None:
        with self._write():
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
        rows = self.conn.execute(
            "SELECT time, symbol, quantity, price, fee FROM fills ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [Fill(_parse(t), s, q, p, f) for t, s, q, p, f in reversed(rows)]

    def add_order(
        self,
        time: datetime,
        symbol: str,
        quantity: float,
        status: str,
        note: str = "",
        client_id: str = "",
    ) -> None:
        with self._write():
            self.conn.execute(
                "INSERT INTO orders (time, symbol, quantity, status, note, client_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_iso(time), symbol, quantity, status, note, client_id),
            )

    def client_id_used(self, client_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM orders WHERE client_id = ? LIMIT 1", (client_id,)
        ).fetchone()
        return row is not None

    def unresolved_orders(self) -> list[OrderRecord]:
        """Orders sent, or maybe sent, whose outcome was never recorded (a crash, an outage)."""
        marks = ", ".join("?" * len(RESOLVED))
        rows = self.conn.execute(
            "SELECT time, symbol, quantity, status, client_id, MIN(id) FROM orders o "
            "WHERE status IN ('pending', 'unknown') AND client_id != '' AND NOT EXISTS ("
            f"SELECT 1 FROM orders r WHERE r.client_id = o.client_id AND r.status IN ({marks})) "
            "GROUP BY client_id ORDER BY MIN(id)",
            RESOLVED,
        ).fetchall()
        return [OrderRecord(_parse(t), s, q, st, c) for t, s, q, st, c, _ in rows]

    # adjustments: reconciliation deltas applied on top of fills

    def add_adjustment(self, adjustment: Adjustment) -> None:
        with self._write():
            self.conn.execute(
                "INSERT INTO adjustments (time, symbol, quantity, cash, note) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    _iso(adjustment.time),
                    adjustment.symbol,
                    adjustment.quantity,
                    adjustment.cash,
                    adjustment.note,
                ),
            )

    def adjustments(self) -> list[Adjustment]:
        rows = self.conn.execute(
            "SELECT time, symbol, quantity, cash, note FROM adjustments ORDER BY id"
        ).fetchall()
        return [Adjustment(_parse(t), s, q, c, n) for t, s, q, c, n in rows]

    # signals and equity

    def add_signals(self, time: datetime, strategy: str, targets: Mapping[str, float]) -> None:
        rows = [(_iso(time), strategy, symbol, target) for symbol, target in targets.items()]
        with self._write():
            self.conn.executemany(
                "INSERT INTO signals (time, strategy, symbol, target) VALUES (?, ?, ?, ?)", rows
            )

    def add_equity(self, point: EquityPoint) -> None:
        with self._write():
            self.conn.execute(
                "INSERT INTO equity (time, equity, cash, exposure) VALUES (?, ?, ?, ?)",
                (_iso(point.time), point.equity, point.cash, point.exposure),
            )

    def equity_points(self) -> list[EquityPoint]:
        rows = self.conn.execute(
            "SELECT time, equity, cash, exposure FROM equity ORDER BY id"
        ).fetchall()
        return [EquityPoint(_parse(t), e, c, x) for t, e, c, x in rows]

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
        with self._write():
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
