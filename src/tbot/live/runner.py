"""Paper, testnet, and live sessions: one event loop, different executors."""

import asyncio
import contextlib
import ctypes
import hashlib
import json
import os
import secrets
import shutil
import signal
import sys
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import structlog

from tbot import __version__
from tbot.backtest.runner import WARMUP_MARGIN, history_bars
from tbot.core.config import TradingConfig, load_yaml
from tbot.core.filelock import lock_file
from tbot.core.models import Fill
from tbot.core.text import clock_text, price_text
from tbot.core.timeframe import Timeframe, to_millis
from tbot.data.downloader import NotListed, sync
from tbot.data.http import describe_error
from tbot.data.store import BarStore
from tbot.exchange.binance import (
    INVALID_SYMBOL,
    KEY_ERRORS,
    PRODUCTION_URL,
    TESTNET_URL,
    BinanceError,
    BinanceSpot,
    SymbolRules,
)
from tbot.execution.rebalance import RebalanceRules
from tbot.execution.sim_broker import SimulatedBroker
from tbot.live.backup import backup_loop, daily_backups
from tbot.live.clock import CLOSE_GRACE, ServerClock
from tbot.live.commands import (
    HELP,
    RANK_REAL_MONEY,
    Answer,
    PollTurn,
    command_loop,
    command_of,
)
from tbot.live.config import (
    ONE_QUOTE,
    LiveConfig,
    PaperConfig,
    SessionConfig,
    Settings,
    looks_like_session,
    named_ledger,
)
from tbot.live.executor import BinanceBookTicker, Executor, LiveExecutor, PaperExecutor
from tbot.live.feed import Batch, LiveFeed, bars_since, close_time, utc_now
from tbot.live.history import MAX_BARS, BarHistory
from tbot.live.ledger import Adjustment, Ledger, config_hash
from tbot.live.reconcile import reconcile
from tbot.live.release import newest_release, update_note
from tbot.live.session import Checkpoint, StreamKey, TradingSession, replay_history
from tbot.monitoring.heartbeat import heartbeat_loop
from tbot.monitoring.telegram import (
    LogNotifier,
    Notifier,
    QueuedNotifier,
    Telegram,
    migrated_to,
)
from tbot.portfolio.allocation import StrategySlot, build_slots
from tbot.portfolio.portfolio import Portfolio
from tbot.risk.guard import Decision, GuardState, Mode, RiskGuard
from tbot.risk.limits import RiskLimits

log = structlog.get_logger(__name__)
CLOCK_RESYNC_SECONDS = 600
SYNC_LOOKBACK_BARS = 400
SHUTDOWN_GRACE_SECONDS = 60  # an event in progress may finish its orders before shutdown
ALERT_FLUSH_SECONDS = 20  # alerts still queued at the end get this long to go out
STOP_POLL_SECONDS = 2.0  # how often a session looks for a `tbot stop` request
STOP_REQUEST_SECONDS = 1200  # a request this fresh also stops a start: beyond the supervisor's
# longest wait between restarts (15 minutes), so a restart after `tbot stop` ends at once
RECONCILE_ALERT_AFTER = 3  # consecutive failed reconciliations before trading pauses
HALT_RETRY_SECONDS = 60.0  # a halted book that still holds coins sells again this soon
HALT_RETRY_MAX_SECONDS = 900.0  # doubling after each failed try, up to this
DAY_REFERENCE = timedelta(hours=36)  # an older snapshot is no reference for a 24h change
GUARD_META = "guard"
BUDGET_META = "initial_cash"  # the budget the guard levels refer to
BASE_META = "base_cash"  # what the book started with
SUPERVISED_ENV = "TBOT_SUPERVISED"  # set by scripts/run_bot.ps1
SUPERVISED_META = "supervised"  # "1": the last start came from a supervisor
BUDGET_NOTE = "initial_cash changed"  # adjustments that book a changed budget
OWED_META = "budget_owed"  # what a lowered budget took beyond the book's cash (<= 0)
# The book's value after the last transfer, with the equity snapshot it follows.
MOVED_META = "equity_after_transfer"
# A live budget change the guard is rescaled for once the first reconciliation has booked
# what traded meanwhile (stops, orders in doubt).
SHIFT_META = "budget_shift_pending"
HEARTBEAT_URL_HINT = "TBOT_HEARTBEAT_URL must be a full http(s):// URL"
MODE_META = "mode"  # paper, testnet, or live: what the book is
MINIMUM_FACTOR = 100  # rebalance.min_notional this many times Binance's: set in another unit
EXIT_REFUSED = 4  # as the command line's config error: the supervisor gives up
KEYS_MISSING = (
    "no Binance API keys: `tbot keys testnet` or `tbot keys live` asks for them, checks them, "
    "and saves them in .env (as TBOT_BINANCE_TESTNET_API_KEY and _SECRET, or "
    "TBOT_BINANCE_API_KEY and _SECRET); testnet keys come from testnet.binance.vision, live "
    "keys from API Management on binance.com"
)
RELEASE_FIRST = 600.0  # seconds after a start before asking whether a newer release is out
RELEASE_EVERY = 86400.0
KEY_REJECTED = (
    "check {} in .env; tbot signs with HMAC, so the key must be a system-generated (HMAC) "
    "one, not Ed25519 or RSA"
)
MAIN_KEYS = "TBOT_BINANCE_API_KEY and TBOT_BINANCE_API_SECRET"
KEY_REFUSED = -2015  # wrong key, wrong network, missing permission, or IP not allowed
KEY_REFUSED_HINT = (
    "the key is mistyped, made for the other network (testnet keys work only on testnet), "
    "lacks spot trading permission, or allows only other IP addresses"
)


def key_hint(exc: Exception, names: str = MAIN_KEYS) -> str:
    """What to check when Binance refuses the key, or "". `names` are the .env lines of
    the pair the session uses."""
    code = getattr(exc, "code", None)
    if code in KEY_ERRORS:
        return KEY_REJECTED.format(names)
    return KEY_REFUSED_HINT if code == KEY_REFUSED else ""


def session_label(mode: str, name: str = "") -> str:
    """How alerts name a session: its mode, and its config when that has another name."""
    stem = Path(name).stem if name else ""
    return mode if not stem or stem == mode else f"{mode} {stem}"


def resume_command(name: str = "") -> str:
    return f"tbot resume {name}" if name else "tbot resume"


TARGETS_META = "targets"
HASHED = {"costs", "risk", "rebalance", "strategies", "guard"}  # what a backtest runs
OPERATIONAL = {"ledger", "backup_days", "telegram_commands"}  # left out of hashes before 0.10.0
HALT_BY_HAND = "halted by hand (`tbot halt`)"
HALT_BY_TELEGRAM = "halted from Telegram (/halt)"
HALT_CONFIRM_SECONDS = 120.0  # a /halt code is good for this long
TELEGRAM_REQUEST = "telegram"  # a halt request left by /halt in another session
SESSIONS_DIR = "sessions"  # in the data folder: the ledger and config of each session run
ALERTS_STUCK_SECONDS = 900.0  # an alert not delivered this long stops the heartbeat
REFUSAL_PROBE_SECONDS = 300.0  # how often to ask whether Telegram takes alerts again
LOW_DISK_BYTES = 2 * 1024**3  # free space below this is alerted and named in the summary
DISK_CHECK_SECONDS = 600.0
CLOCK_NOTE_SECONDS = 30.0  # a PC clock this far off the exchange's is named in the summary
FINAL_PROTECT_SECONDS = 30.0  # at the end, a stop a cut-short event left out gets this long
ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001  # SetThreadExecutionState flags
_background: set[asyncio.Task[bool]] = set()


class AlreadyRunning(RuntimeError):
    """Another process holds this session's ledger."""


@dataclass
class Health:
    """A condition outside the risk guard that pauses new orders; empty when all is well."""

    blocked: str = ""


class SessionTrader:
    """Turns each batch of closed bars into decisions, guarded orders, and ledger rows."""

    def __init__(
        self,
        session: TradingSession,
        ledger: Ledger,
        executor: Executor,
        notifier: Notifier,
        guard: RiskGuard,
        clock: Callable[[], datetime] = utc_now,
        label: str = "paper",
        lock: asyncio.Lock | None = None,
        health: Health | None = None,
        name: str = "",
    ) -> None:
        self.session = session
        self.ledger = ledger
        self.executor = executor
        self.notifier = notifier
        self.guard = guard
        self.clock = clock
        self.label = label
        self.lock = lock or asyncio.Lock()  # one writer to the book at a time
        self.health = health or Health()
        self.resume_command = resume_command(name)
        self._paused = False  # a blocked bar was announced; later ones are only logged
        self._reduced: date | None = None  # the UTC day "entries blocked" was last sent
        self._halted_seen = guard.state.halted  # a change to not halted is a resume
        self._handled: tuple[datetime | None, set[StreamKey]] = (None, set())
        # Streams a close may never bring (stale, or a pair Binance does not trade): the
        # checkpoint does not wait for them.
        self.quiet: Callable[[], set[StreamKey]] = set

    async def handle(self, batch: Batch) -> list[Fill]:
        async with self.lock:
            return await self._handle(batch)

    async def _handle(self, batch: Batch) -> list[Fill]:
        session = self.session
        now = max(close_time(key, bar) for key, bar in batch.items())
        await self.executor.before_event(session.portfolio)  # a stop that sold meanwhile
        orders = session.ingest(batch, now)
        for slot in session.slots:
            if any((s, slot.timeframe) in batch for s in slot.strategy.symbols):
                self.ledger.add_signals(now, slot.strategy.name, slot.targets)

        self.reload_guard()
        was_halted = self.guard.state.halted
        if self._halted_seen and not was_halted:  # `tbot resume` while this session runs
            await self.notifier.send(
                f"[{self.label}] resumed: the kill switch is cleared, trading again from this bar"
            )
        decision = self.guard.check(self.clock(), session.portfolio.equity(session.marks), now)
        if decision.mode != Mode.HALT and self.health.blocked:
            decision = Decision(Mode.BLOCK, self.health.blocked)
        self.save_guard()
        self._halted_seen = self.guard.state.halted
        fills: list[Fill] = []
        if decision.mode == Mode.HALT:
            fills = await self._halt(decision.reason, now, announce=not was_halted)
        else:
            if decision.mode == Mode.BLOCK:
                self.ledger.add_event(now, "warning", f"blocked: {decision.reason}")
                if not self._paused:
                    await self.notifier.send(
                        f"[{self.label}] no buying this bar: {decision.reason}; sales still go "
                        f"out. Further blocked bars are logged, not sent."
                    )
                self._paused = True
            elif self._paused:
                self._paused = False
                await self.notifier.send(f"[{self.label}] trading again")
            # A stale bar or a failing reconciliation holds back buys, never an exit.
            mode = Mode.REDUCE_ONLY if decision.mode == Mode.BLOCK else decision.mode
            allowed = self.guard.filter_orders(orders, session.portfolio.positions, mode)
            if decision.mode == Mode.REDUCE_ONLY and allowed != dict(orders):
                self.ledger.add_event(now, "warning", f"reduce only: {decision.reason}")
                if self._reduced != now.date():  # a daily loss limit holds for the day
                    self._reduced = now.date()
                    await self.notifier.send(
                        f"[{self.label}] entries blocked: {decision.reason}; more today are "
                        "logged, not sent"
                    )
            if allowed or decision.mode != Mode.BLOCK:
                fills = await self.executor.execute(allowed, now, session.portfolio, session.marks)
                await self.executor.after_event(session.portfolio, session.marks)
        self.ledger.add_equity(session.snapshot(now))
        if self._event_done(batch, now):  # last: a crash before it hands this bar over again
            self.save_checkpoint(now)
        log.info(
            "bar_event",
            time=now.isoformat(),
            streams=[f"{s}:{tf}" for s, tf in batch],
            mode=str(decision.mode),
            orders=len(orders),
            fills=len(fills),
            equity=round(session.portfolio.equity(session.marks), 2),
        )
        return fills

    async def _halt(self, reason: str, now: datetime, *, announce: bool) -> list[Fill]:
        """Kill switch: flatten, then stay out until a human resumes.

        Announced when tripped and whenever something fills, not on every bar:
        dust below the exchange minimum can stay behind for a long time.
        """
        session = self.session
        orders = self.guard.flatten_orders(self.sellable())
        fills: list[Fill] = []
        if orders:
            fills = await self.executor.execute(orders, now, session.portfolio, session.marks)
            await self.executor.after_event(session.portfolio, session.marks)
        if announce or fills:
            dust = unsold = 0
            for symbol, quantity in session.portfolio.positions.items():
                value = abs(quantity) * session.marks.get(symbol, 0.0)
                if quantity and value < session.config.rebalance.min_notional:
                    dust += 1
                elif quantity:
                    unsold += 1
            rest = f", {dust} left below the exchange minimum" if dust else ""
            if unsold:
                rest += f", {unsold} NOT SOLD (see the alerts above)"
            self.ledger.add_event(now, "error", f"kill switch: {reason}")
            await self.notifier.send(
                f"[{self.label}] KILL SWITCH: {reason}. Positions flattened ({len(fills)} "
                f"fills{rest}). Trading stays halted until `{self.resume_command}`."
            )
        self._halted_seen = True
        return fills

    async def halt_now(self, reason: str) -> list[Fill]:
        """The panic button: sell what the book holds now, not at the next bar, and trade
        no more until `tbot resume`. A session already halted sells what is left."""
        async with self.lock:
            session = self.session
            await self.executor.before_event(session.portfolio)  # a stop that sold meanwhile
            self.reload_guard()
            announce = not self.guard.state.halted
            if announce:
                self.guard.halt(reason)
                self.save_guard()
                log.warning("halted", reason=reason)
            now = self.clock()
            return await self._halt(self.guard.state.halt_reason, now, announce=announce)

    def sellable(self) -> dict[str, float]:
        """Positions an order can sell: below the exchange minimum is dust, which stays."""
        session = self.session
        minimum = session.config.rebalance.min_notional
        return {
            symbol: quantity
            for symbol, quantity in session.portfolio.positions.items()
            if quantity
            and (symbol not in session.marks or abs(quantity) * session.marks[symbol] >= minimum)
        }

    async def sell_if_halted(self) -> list[Fill]:
        """A halted book that still holds coins (`tbot halt` while stopped, a sale that
        failed) sells them now rather than at the next bar."""
        if not self.sellable():
            return []
        async with self.lock:
            self.reload_guard()  # a `tbot resume` while this session started
            if not self.guard.state.halted:
                return []
            return await self._halt(self.guard.state.halt_reason, self.clock(), announce=False)

    def _event_done(self, batch: Batch, now: datetime) -> bool:
        """Every stream closing at now has been handled, maybe over two events (a late one).

        Until then the checkpoint stays at the event before, so a restart hands every bar
        of this close over again instead of replaying the ones that never got their event.
        """
        if self._handled[0] != now:
            self._handled = (now, set())
        self._handled[1].update(batch)
        due = {key for key in self.session.keys if to_millis(now) % key[1].millis == 0}
        return due - self.quiet() <= self._handled[1]

    def save_guard(self) -> None:
        self.ledger.set_meta(GUARD_META, self.guard.state.model_dump_json())

    def reload_guard(self) -> None:
        adopt_resume(self.guard, self.ledger)

    def save_checkpoint(self, now: datetime) -> None:
        """Strategy targets after this event, so a restart continues from them."""
        checkpoint = self.session.checkpoint(now)
        payload = {
            "time": now.isoformat(),
            "strategies": strategies_hash(self.session.config),
            "targets": checkpoint.targets,
        }
        self.ledger.set_meta(TARGETS_META, json.dumps(payload))


def strategies_hash(config: TradingConfig) -> str:
    return config_hash(
        json.dumps([s.model_dump(mode="json") for s in config.strategies], sort_keys=True)
    )


def load_checkpoint(config: SessionConfig, ledger: Ledger) -> Checkpoint | None:
    """The last event's strategy targets, if the strategies are still the same."""
    raw = ledger.get_meta(TARGETS_META)
    if raw is None:
        return None
    data = json.loads(raw)
    if data.get("strategies") != strategies_hash(config):
        log.warning("checkpoint_ignored", reason="strategies changed since the last run")
        return None
    targets = [{str(k): float(v) for k, v in slot.items()} for slot in data["targets"]]
    return Checkpoint(datetime.fromisoformat(data["time"]), targets)


def adopt_resume(guard: RiskGuard, ledger: Ledger) -> None:
    """Adopt a `tbot resume` issued while this process runs, before writing the guard."""
    if not guard.state.halted:
        return
    raw = ledger.get_meta(GUARD_META)
    if raw:
        stored = GuardState.model_validate_json(raw)
        if not stored.halted:
            guard.state = stored


def check_budget(
    config: SessionConfig,
    ledger: Ledger,
    guard: RiskGuard,
    now: datetime,
    store: BarStore | None = None,
    at: datetime | None = None,
) -> str:
    """A changed initial_cash moves money into or out of the book: not a profit or loss.

    It is booked as a cash adjustment, like a deposit, so the guard, the dashboard, and
    compare all see a transfer, at `at` (else now; see transfer_time). Run it before the
    book is restored, after the store is synced: the book is valued at its latest closes,
    not at the last bar before a stop. Returns what changed, or "".
    """
    stored = ledger.get_meta(BUDGET_META)
    note = ""
    with ledger.atomic():
        if ledger.get_meta(BASE_META) is None:
            ledger.set_meta(BASE_META, stored if stored is not None else repr(config.initial_cash))
        if stored is not None and float(stored) != config.initial_cash:
            change, equity, problem = budget_change(config, ledger, store, now)
            if problem:
                raise StartRefused(problem)
            # What a cut may leave owing is known once orders in doubt are settled, at the
            # first reconciliation; until then the whole cut counts (note_repaid).
            owed = min(0.0, budget_owed(ledger) + change)
            note = f"{BUDGET_NOTE} from {float(stored):,.2f} to {config.initial_cash:,.2f}"
            ledger.add_adjustment(Adjustment(at or now, "", 0.0, change, note))
            ledger.set_meta(OWED_META, repr(owed))
            if isinstance(config, LiveConfig):  # stops may have sold since the last bar
                ledger.set_meta(SHIFT_META, repr(pending_shift(ledger) + change))
            else:
                guard.shift(change, equity)
                ledger.set_meta(GUARD_META, guard.state.model_dump_json())
                note_transfer(ledger, equity + change)
            ledger.add_event(now, "warning", f"{note}; booked as a transfer")
        ledger.set_meta(BUDGET_META, repr(config.initial_cash))
    return note


def budget_change(
    config: SessionConfig, ledger: Ledger, store: BarStore | None, now: datetime
) -> tuple[float, float, str]:
    """A changed initial_cash: the change, the book's value it applies to, and why the start
    refuses it ("" when it does not): a cut beyond what the book is worth."""
    stored = ledger.get_meta(BUDGET_META)
    if stored is None:
        return 0.0, 0.0, ""
    change = config.initial_cash - float(stored)
    equity = book_value(config, ledger, store, now) if store is not None else None
    if equity is None:
        equity = book_equity(config, ledger) + pending_shift(ledger)
    if equity + change >= 0:
        return change, equity, ""
    return (
        change,
        equity,
        (
            f"initial_cash {config.initial_cash:,.2f} takes {-change:,.2f} out of a book worth "
            f"{equity:,.2f}; the lowest it can be now is {float(stored) - equity:,.2f}"
        ),
    )


def book_equity(config: SessionConfig, ledger: Ledger) -> float:
    """The book's value as last known: the last bar event's, unless a transfer came after."""
    raw = ledger.get_meta(MOVED_META)
    if raw is not None:
        rows, equity = json.loads(raw)
        if rows == ledger.equity_rows():
            return float(equity)
    point = ledger.latest_equity()
    if point is not None:
        return point.equity
    last = {fill.symbol: fill.price for fill in ledger.fills()}  # no bar event valued it yet
    book = restore_portfolio(config, ledger)
    return book.cash + sum(qty * last.get(s, 0.0) for s, qty in book.positions.items())


def book_value(
    config: SessionConfig, ledger: Ledger, store: BarStore, now: datetime
) -> float | None:
    """The book's value now: its cash, and its coins at the latest stored closes. None
    when a coin has no stored price. Budget changes booked earlier are in the cash."""
    book = restore_portfolio(config, ledger)
    value = book.cash
    for symbol, quantity in book.positions.items():
        price = price_at(config, store, symbol, now)
        if quantity and not price > 0:
            return None
        value += quantity * price
    return value


def pending_shift(ledger: Ledger) -> float:
    raw = ledger.get_meta(SHIFT_META)
    return float(raw) if raw is not None else 0.0


def apply_budget_shift(guard: RiskGuard, ledger: Ledger, equity: float) -> None:
    """Rescale the guard for budget changes booked at the start, now that the book holds
    what executed meanwhile; `equity` is the book's value with the changes."""
    change = pending_shift(ledger)
    if not change:
        return
    with ledger.atomic():
        adopt_resume(guard, ledger)
        guard.shift(change, equity - change)
        ledger.set_meta(GUARD_META, guard.state.model_dump_json())
        ledger.delete_meta(SHIFT_META)
        note_transfer(ledger, equity)


def note_transfer(ledger: Ledger, equity: float) -> None:
    """Remember the book's value after a transfer, until the next bar event values it."""
    ledger.set_meta(MOVED_META, json.dumps([ledger.equity_rows(), equity]))


def price_at(config: TradingConfig, store: BarStore, symbol: str, moment: datetime) -> float:
    """Close of the symbol's finest stream at or before moment (the next one if none)."""
    if not symbol:
        return 0.0  # a cash-only adjustment
    configured = {c.timeframe for c in config.strategies if symbol in c.symbols}
    # A symbol the config no longer trades is valued from whatever stream is stored.
    for timeframe in sorted(configured or set(Timeframe), key=lambda t: t.millis):
        bars = store.read(
            symbol, timeframe, moment - timeframe.delta * 50, moment + timeframe.delta
        )
        if bars.is_empty():
            continue
        closed = bars.filter(bars["open_time"] + timeframe.delta <= moment)
        return float(closed["close"][-1]) if not closed.is_empty() else float(bars["close"][0])
    return 0.0


def next_close(config: TradingConfig, now: datetime) -> tuple[Timeframe, datetime]:
    """The finest timeframe the config trades and its next close: the next decision."""
    timeframe = min((c.timeframe for c in config.strategies), key=lambda t: t.millis)
    return timeframe, timeframe.floor(now) + timeframe.delta


def money_put_in(
    config: TradingConfig,
    ledger: Ledger,
    price: Callable[[str, datetime], float],
    until: datetime,
) -> float:
    """What profit is measured against: the cash the book started with, and every deposit,
    withdrawal, and budget change up to `until`, coins valued at their time."""
    return base_cash(config, ledger) + sum(
        a.cash + a.quantity * price(a.symbol, a.time)
        for a in ledger.adjustments()
        if a.time <= until
    )


def budget_owed(ledger: Ledger) -> float:
    """What lowered budgets took beyond the book's cash and sales have not repaid, as a
    negative amount: cash below zero up to this is owed to the owner, not an error for
    reconciliation to undo."""
    raw = ledger.get_meta(OWED_META)
    if raw is not None:
        return float(raw)
    budget = [a for a in ledger.adjustments() if a.note.startswith(BUDGET_NOTE)]  # 0.4.2
    return sum(a.cash for a in budget if a.cash < 0)


def note_repaid(ledger: Ledger, cash: float) -> None:
    """Sales repay what a lowered budget left owing; the book never owes more again.
    Run it after settle: an order in doubt booked later would look like a repayment."""
    owed = budget_owed(ledger)
    left = max(owed, min(0.0, cash))
    if left != owed or ledger.get_meta(OWED_META) is None:
        ledger.set_meta(OWED_META, repr(left))


def base_cash(config: TradingConfig, ledger: Ledger) -> float:
    """Cash the book started with; later changes of initial_cash are adjustments."""
    for key in (BASE_META, BUDGET_META):  # a ledger from 0.2.0 has only the budget
        raw = ledger.get_meta(key)
        if raw is not None:
            return float(raw)
    return config.initial_cash


def unbooked_budget(config: SessionConfig, ledger: Ledger) -> float:
    """A change of initial_cash since the last start; the next start books it."""
    raw = ledger.get_meta(BUDGET_META)
    return config.initial_cash - float(raw) if raw is not None else 0.0


def load_guard(config: SessionConfig, ledger: Ledger) -> RiskGuard:
    raw = ledger.get_meta(GUARD_META)
    state = GuardState.model_validate_json(raw) if raw else GuardState()
    return RiskGuard(config.guard, state)


def stream_keys(config: SessionConfig) -> list[StreamKey]:
    keys = {(symbol, c.timeframe) for c in config.strategies for symbol in c.symbols}
    return sorted(keys, key=lambda key: (key[1].millis, key[0]))


def symbols_of(config: TradingConfig) -> list[str]:
    return sorted({symbol for c in config.strategies for symbol in c.symbols})


def lookback_bars(slots: Sequence[StrategySlot], risk: RiskLimits) -> dict[StreamKey, int]:
    """Bars to replay per stream: what a backtest loads before its start, margin included."""
    return {key: bars * WARMUP_MARGIN for key, bars in history_bars(slots, risk).items()}


def restore_portfolio(config: SessionConfig, ledger: Ledger) -> Portfolio:
    """Fills and reconciliation adjustments, replayed in time order, rebuild the book."""
    portfolio = Portfolio(base_cash(config, ledger), dust_notional=config.rebalance.min_notional)
    events: list[tuple[datetime, int, Fill | Adjustment]] = []
    events.extend((f.time, 0, f) for f in ledger.fills())
    events.extend((a.time, 1, a) for a in ledger.adjustments())
    for _, _, item in sorted(events, key=lambda e: (e[0], e[1])):
        if isinstance(item, Fill):
            portfolio.apply(item)
        else:
            portfolio.adjust(item.symbol, item.quantity, item.cash)
    return portfolio


def transfer_time(
    config: SessionConfig, ledger: Ledger, checkpoint: Checkpoint | None, now: datetime
) -> datetime:
    """When money found at a start (a changed budget, a deposit while it was down) is
    booked: after the ledger's last bar event, before any bar the start hands over, so no
    equity row shows it on the wrong side as a profit or loss."""
    at = replay_until(config, checkpoint, now)
    point = ledger.latest_equity()
    if point is not None and point.time >= at:
        at = point.time + timedelta(milliseconds=1)
    return min(at, now)


def replay_until(config: SessionConfig, checkpoint: Checkpoint | None, now: datetime) -> datetime:
    """Where the start-up replay ends; bars that closed later reach the session as events.

    Bars after the last event handled that closed recently enough to trade (within the
    guard's stale window) are traded, not replayed: a restart during a bar close, or a
    crash in the middle of an event, must not lose that bar's orders.
    """
    if checkpoint is None:
        return now
    window = config.guard.stale_seconds or config.stale_after_seconds
    return min(now, max(checkpoint.time, now - timedelta(seconds=window)))


def build_session(
    config: SessionConfig,
    store: BarStore,
    portfolio: Portfolio,
    now: datetime,
    checkpoint: Checkpoint | None = None,
) -> TradingSession:
    slots = build_slots(config.strategies)
    lookback = lookback_bars(slots, config.risk)
    until = replay_until(config, checkpoint, now)
    frames: dict[StreamKey, pl.DataFrame] = {}
    for key, count in lookback.items():
        # The first event brings its own bar: with it the strategies see `count` bars, as
        # a backtest starting at that event does (its first bar included).
        frames[key] = bars_since(store, key, count - 1, until)
        if frames[key].height < count // WARMUP_MARGIN:
            hint = store.download_hint([key])
            raise ValueError(f"not enough stored bars for {key[0]} {key[1]}; run {hint}")
    histories = {
        key: BarHistory(key[1], max_bars=max(MAX_BARS, count)) for key, count in lookback.items()
    }
    session = TradingSession(config, slots, histories, portfolio)
    mark_leftovers(session, store, now)
    replay_history(session, frames, checkpoint)
    return session


def leftovers(
    config: TradingConfig, portfolio: Portfolio, store: BarStore, now: datetime
) -> tuple[dict[str, float], str]:
    """Coins of pairs the config no longer trades: the marks of dust a sale left behind (no
    order can sell it, so it is left alone), and why the start is refused when there is
    more, or "". Without stored bars of the pair (dropped long ago), its last fill prices it."""
    traded = set(symbols_of(config))
    last = {fill.symbol: fill.price for fill in portfolio.fills}
    marks: dict[str, float] = {}
    for symbol, quantity in portfolio.positions.items():
        if not quantity or symbol in traded:
            continue
        price = price_at(config, store, symbol, now) or last.get(symbol, 0.0)
        if price > 0 and abs(quantity) * price < config.rebalance.min_notional:
            marks[symbol] = price
            continue
        return marks, (
            f"the book holds {quantity:.8f} {symbol}, which this config does not trade: add "
            "the symbol back to the config (and run `tbot download`) so the bot can value, "
            "manage, and sell it"
        )
    return marks, ""


def mark_leftovers(session: TradingSession, store: BarStore, now: datetime) -> None:
    """Value the dust of pairs the config no longer trades; refuse more than dust."""
    marks, problem = leftovers(session.config, session.portfolio, store, now)
    if problem:
        raise StartRefused(problem)
    session.marks.update(marks)


def summary_text(
    session: TradingSession,
    ledger: Ledger,
    now: datetime,
    label: str,
    title: str = "daily summary",
    notes: Sequence[str] = (),
    store: BarStore | None = None,
    resume: str = "tbot resume",
) -> str:
    """Equity, its change, profit, positions, and fills; coins moved in or out of the book
    are valued at their time from the store, as `tbot status` does, else at today's marks."""
    equity = session.portfolio.equity(session.marks)
    earlier = ledger.equity_before(now - timedelta(days=1))
    change = "no 24h reference"
    span = "over 24h"
    if earlier and earlier.time < now - DAY_REFERENCE:  # the bot was down: say since when
        span = f"since {earlier.time:%Y-%m-%d %H:%M}"

    def price(symbol: str, moment: datetime) -> float:
        if store is None:
            return session.marks.get(symbol, 0.0)
        return price_at(session.config, store, symbol, moment)

    if earlier and earlier.equity > 0:
        moved = sum(  # deposits, withdrawals, budget changes: not a gain or a loss
            a.cash + a.quantity * price(a.symbol, a.time)
            for a in ledger.adjustments()
            if earlier.time < a.time <= now
        )
        change = f"{(equity - moved) / earlier.equity - 1:+.2%} {span}"
    raw = ledger.get_meta(GUARD_META)
    guard = GuardState.model_validate_json(raw) if raw else GuardState()
    if guard.peak_equity > 0:
        change += f", {min(equity / guard.peak_equity - 1, 0.0):.1%} from peak"
    positions = ", ".join(
        f"{symbol} {qty:.6f} ({qty * session.marks.get(symbol, 0.0):,.0f})"
        for symbol, qty in sorted(session.portfolio.positions.items())
    )
    invested = money_put_in(session.config, ledger, price, now)
    share = f" ({(equity - invested) / invested:+.1%})" if invested > 0 else ""
    lines = [
        f"[{label}] {title} {now:%Y-%m-%d %H:%M} UTC",
        f"equity {equity:,.2f} ({change}), cash {session.portfolio.cash:,.2f}",
        f"profit {equity - invested:+,.2f}{share} on {invested:,.2f} put in",
        f"positions: {positions or 'none'}",
        f"fills in 24h: {ledger.fill_count(now - timedelta(days=1))}",
    ]
    point = ledger.latest_equity()
    if point is not None:  # marks move only at bar events
        lines.append(f"prices of the last bar event, {point.time:%Y-%m-%d %H:%M} UTC")
    if guard.halted:
        lines.append(f"HALTED: {guard.halt_reason}; `{resume}` to trade again")
    lines.extend(notes)
    return "\n".join(lines)


def fills_text(ledger: Ledger, label: str, count: int = 10) -> str:
    fills = ledger.recent_fills(count)
    if not fills:
        return f"[{label}] no fills yet"
    return "\n".join(
        [f"[{label}] last {len(fills)} fills (UTC)"]
        + [
            f"{f.time:%m-%d %H:%M} {'BUY' if f.quantity > 0 else 'SELL'} "
            f"{abs(f.quantity):.6f} {f.symbol} @ {price_text(f.price)}"
            for f in fills
        ]
    )


def held_text(portfolio: Portfolio, marks: Mapping[str, float], minimum: float = 0.0) -> str:
    """What the book holds; dust below `minimum` (the exchange minimum) is no holding."""
    held = [
        f"{symbol} {quantity:.6f} ({quantity * marks.get(symbol, 0.0):,.2f})"
        for symbol, quantity in sorted(portfolio.positions.items())
        if quantity and (symbol not in marks or abs(quantity) * marks[symbol] >= minimum)
    ]
    return f"still held: {', '.join(held)}" if held else "nothing held"


def held_positions(config: SessionConfig, ledger: Ledger) -> dict[str, float]:
    """What the book holds, without dust: a position worth less than the exchange minimum
    at its last fill price can never be sold, so no message counts it."""
    last = {fill.symbol: fill.price for fill in ledger.fills()}
    minimum = config.rebalance.min_notional
    return {
        symbol: quantity
        for symbol, quantity in restore_portfolio(config, ledger).positions.items()
        if quantity and (symbol not in last or abs(quantity) * last[symbol] >= minimum)
    }


def command_answer(
    session: TradingSession,
    ledger: Ledger,
    label: str,
    clock: Callable[[], datetime],
    notes: Callable[[], list[str]],
    store: BarStore | None = None,
    halt: Callable[[str], Awaitable[list[Fill]]] | None = None,
    name: str = "",
    others: Callable[[], list[tuple[str, Path, bool]]] = list,
) -> Answer:
    """`others` names the other sessions to halt with this one, by name, ledger, and
    whether they run: only one session reads the bot's messages."""
    code: list[tuple[str, float]] = []  # the /halt code in force, and when it expires
    resume = resume_command(name)
    halt_command = f"tbot halt {name}" if name else "tbot halt"

    async def answer(text: str) -> str:
        command = command_of(text)
        if command in ("/status", "/start"):
            return summary_text(session, ledger, clock(), label, "status", notes(), store, resume)
        if command == "/fills":
            return fills_text(ledger, label)
        if command == "/halt" and halt is not None:
            return await halt_answer(halt, text.split()[1:2])
        return HELP

    async def halt_answer(halt: Callable[[str], Awaitable[list[Fill]]], given: list[str]) -> str:
        """Two steps, so a tap on /halt in a message never sells: the second one must
        carry the code the first one answered."""
        expected, deadline = code.pop() if code else ("", 0.0)  # one try per code
        if not given:
            new = f"{secrets.randbelow(10_000):04d}"
            code.append((new, time.monotonic() + HALT_CONFIRM_SECONDS))
            return (
                f"[{label}] /halt sells everything the bot holds at the market price and "
                f"stops its trading until `{resume}` on the computer. To go on, send "
                f"/halt {new} within {HALT_CONFIRM_SECONDS / 60:.0f} minutes."
            )
        if given[0] != expected or time.monotonic() > deadline:
            return f"[{label}] wrong or expired code, nothing done: send /halt for a new one"
        asked: list[str] = []
        later: list[str] = []  # not running: a supervisor may start them, or they hold coins
        missed: list[str] = []
        for other, path, running in others():  # first: they sell while this one does
            try:
                halt_path(path).write_text(f"{TELEGRAM_REQUEST}\n", encoding="utf-8")
                (asked if running else later).append(other)
            except OSError as exc:
                log.error("halt_request_failed", session=other, error=repr(exc))
                missed.append(other)
        also = f" Also halting: {', '.join(asked)}; their alerts follow." if asked else ""
        if later:
            also += f" Halting at their next start: {', '.join(later)}."
        if missed:
            also += f" NOT REACHED: {', '.join(missed)}; on the computer: " + ", ".join(
                f"`tbot halt {other}`" for other in missed
            )
        try:
            fills = await halt(HALT_BY_TELEGRAM)
        except Exception as exc:  # the owner must know the bot did not get out
            log.exception("halt_failed")
            return f"[{label}] halt FAILED: {exc!r}; run `{halt_command}` on the computer.{also}"
        minimum = session.config.rebalance.min_notional
        held = held_text(session.portfolio, session.marks, minimum)
        return f"[{label}] halted: {len(fills)} fills, {held}. No trading until `{resume}`.{also}"

    return answer


async def summary_loop(
    session: TradingSession,
    ledger: Ledger,
    notifier: Notifier,
    hour: int,
    label: str,
    notes: Callable[[], list[str]] = list,
    store: BarStore | None = None,
    resume: str = "tbot resume",
) -> None:
    sent: date | None = None  # a wall clock stepped back must not send a day twice
    while True:
        now = utc_now()
        target = now.replace(hour=hour, minute=5, second=0, microsecond=0)
        if target <= now or target.date() == sent:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        if utc_now() < target:
            continue  # the wall clock moved back while sleeping: wait the rest
        sent = target.date()
        try:
            text = summary_text(
                session, ledger, utc_now(), label, notes=notes(), store=store, resume=resume
            )
            await notifier.send(text)
        except Exception as exc:  # a report must never stop trading
            log.error("summary_failed", error=repr(exc))


async def release_loop(found: list[str]) -> None:
    """Once a day, see whether a newer release is out; the daily summary says so."""
    await asyncio.sleep(RELEASE_FIRST)
    while True:
        note = update_note(await asyncio.to_thread(newest_release))
        if note:
            found[:] = [note]
        await asyncio.sleep(RELEASE_EVERY)


async def clock_loop(clock: ServerClock, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(clock.sync)
        except (httpx.HTTPError, LookupError, ValueError) as exc:
            log.warning("clock_sync_failed", error=repr(exc))


async def halt_retry_loop(trader: SessionTrader) -> None:
    """A halted book whose sale failed (a rate limit, an outage) tries again within
    minutes instead of at the next close, which can be a day away."""
    wait = HALT_RETRY_SECONDS
    while True:
        await asyncio.sleep(wait)
        if not (trader.guard.state.halted and trader.sellable()):
            wait = HALT_RETRY_SECONDS
            continue
        log.warning("halt_retry", positions=trader.sellable())
        try:
            await trader.sell_if_halted()
        except Exception as exc:  # the next try, or the next bar, sells
            log.error("halt_retry_failed", error=repr(exc))
        wait = min(wait * 2, HALT_RETRY_MAX_SECONDS)


async def consume(feed: LiveFeed, trader: SessionTrader) -> None:
    async for batch in feed.batches():
        await trader.handle(batch)


def make_notifier(settings: Settings, client: httpx.AsyncClient) -> Notifier:
    if settings.telegram_token and settings.telegram_chat_id:
        return Telegram(settings.telegram_token, settings.telegram_chat_id, client)
    log.warning("telegram_not_configured", hint="alerts go to the log; `tbot notify` sets it up")
    return LogNotifier()


def install_stop_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows event loops
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


def start_problem(exc: Exception) -> str:
    """A failed start in a line: a network error names the host and the answer, never the URL."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        answer = exc.response.text.strip()
        problem = f"HTTP {status} from {exc.request.url.host}"
        if answer.startswith("{"):  # an exchange error such as {"code":-2015,"msg":...}
            problem += f": {answer[:200]}"
        passing = status >= 500 or status in (403, 418, 429)  # 403: Binance's firewall
    elif isinstance(exc, httpx.HTTPError):
        problem, passing = f"network error {type(exc).__name__}", True
    else:
        return str(exc) or repr(exc)
    if passing:
        return f"{problem}; often brief: a supervised bot tries again, by hand start it again"
    return (
        f"{problem}; a supervised bot tries again, but if it keeps failing, check the API key, "
        "its IP restriction, and the network"
    )


class StartRefused(ValueError):
    """A start that fails the same way every time: the config or .env needs a fix."""


def permanent(exc: Exception) -> bool:
    """Whether a failed start would fail again: a supervisor should give up, not retry."""
    if isinstance(exc, StartRefused | NotListed):
        return True
    return isinstance(exc, BinanceError) and (exc.code == INVALID_SYMBOL or exc.code in KEY_ERRORS)


class LedgerModeError(ValueError):
    """The ledger keeps the book of another mode."""


def session_mode(config: SessionConfig) -> str:
    return config.mode if isinstance(config, LiveConfig) else "paper"


def ledger_mode(ledger: Ledger) -> str | None:
    """The mode the ledger's book was kept in: recorded since 0.4.2, else its first start."""
    mode = ledger.get_meta(MODE_META)
    if mode is not None:
        return mode
    row = ledger.conn.execute(
        "SELECT message FROM events WHERE message LIKE 'started (%' ORDER BY id LIMIT 1"
    ).fetchone()
    return str(row[0]).removeprefix("started (").split(")", 1)[0] if row else None


def mode_problem(config: SessionConfig, ledger: Ledger) -> str:
    """Why this config must not use this ledger, or "": a testnet or paper book must never
    trade real money, and simulated fills must never enter a real-money book."""
    mode, kept = session_mode(config), ledger_mode(ledger)
    if kept is None or kept == mode:
        return ""
    named = Path("data") / f"{mode}.sqlite"
    if named.resolve() == config.ledger.resolve():  # that is the one taken
        named = named.with_name(f"{mode}-2.sqlite")
    return (
        f"{config.ledger} keeps a {kept} book, not a {mode} one: give this config a ledger "
        f"of its own (`ledger: {named.as_posix()}` or another new file)"
    )


def stop_path(ledger: Path) -> Path:
    """`tbot stop` leaves this file: a graceful stop for a bot that has no console."""
    return ledger.with_name(ledger.name + ".stop")


def take_stop_request(ledger: Path) -> bool:
    """Remove a request no session has taken yet; True if it is fresh enough to obey.

    A fresh one stops a session as it starts, so a supervisor that restarts a crashed or
    waiting bot after `tbot stop` ends instead of trading on.
    """
    path = stop_path(ledger)
    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return False
    with contextlib.suppress(PermissionError):  # held open by `tbot stop` or a scanner
        path.unlink(missing_ok=True)
    return age < STOP_REQUEST_SECONDS


def supervised() -> bool:
    """Started by scripts/run_bot.ps1, which restarts the bot after a crash."""
    return os.environ.get(SUPERVISED_ENV) == "1"


def is_running(ledger: Path, wait: float = 0.0) -> bool:
    try:
        with instance_lock(ledger, wait):
            return False
    except AlreadyRunning:
        return True


def halt_path(ledger: Path) -> Path:
    """`tbot halt` leaves this file for a running session; it is removed once done."""
    return ledger.with_name(ledger.name + ".halt")


async def watch_halt_request(path: Path, halt: Callable[[str], Awaitable[object]]) -> None:
    """Halt when `tbot halt` asks. The request goes once the halt is saved and the sales
    were tried, so a crash in between halts again at the next start; what a failed sale
    left, the session sells again within minutes (halt_retry_loop)."""
    while True:
        if path.exists():
            try:
                by = path.read_text(encoding="utf-8").strip()
            except OSError:  # being written, or still open elsewhere: the reason is all
                by = ""
            log.warning("halt_requested", by=by or "tbot halt")
            await halt(HALT_BY_TELEGRAM if by == TELEGRAM_REQUEST else HALT_BY_HAND)
            while path.exists():
                try:
                    path.unlink(missing_ok=True)
                except PermissionError:  # Windows: still open in another process
                    await asyncio.sleep(STOP_POLL_SECONDS)
        await asyncio.sleep(STOP_POLL_SECONDS)


@dataclass(frozen=True)
class Registered:
    """A ledger a session of this clone ran on, and the config it started from."""

    ledger: Path
    config: Path | None
    name: str  # the config as typed at the start

    @property
    def label(self) -> str:
        return self.config.stem if self.config else Path(self.name).stem or self.ledger.stem


def register_session(data_dir: Path, ledger: Path, source: Path | None, name: str) -> None:
    """Note this session's ledger and config: `tbot halt`, `tbot stop`, and /halt find it
    there also after its config was edited into errors or to name another ledger."""
    where = ledger.resolve()
    key = hashlib.sha256(str(where).lower().encode()).hexdigest()[:16]
    path = data_dir / SESSIONS_DIR / f"{key}.json"
    entry = {"ledger": str(where), "config": str(source.resolve()) if source else "", "name": name}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_suffix(".part")
        part.write_bytes(json.dumps(entry).encode("utf-8"))
        part.replace(path)
    except OSError as exc:  # the scan of config/ still finds it
        log.warning("session_not_registered", error=repr(exc))


def registered_sessions(data_dir: Path) -> list[Registered]:
    """The sessions this clone ran whose ledgers are still there."""
    found = []
    for path in sorted((data_dir / SESSIONS_DIR).glob("*.json")):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            ledger = Path(entry["ledger"])
            config = Path(entry["config"]) if entry.get("config") else None
            name = str(entry.get("name", ""))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if ledger.is_file():
            found.append(Registered(ledger, config, name))
    return found


def running_ledger(data_dir: Path, config: Path) -> Path | None:
    """The ledger a running session started from this config uses, whatever the file says
    now (edited into errors, or to another ledger)."""
    for entry in registered_sessions(data_dir):
        if entry.config == config.resolve() and is_running(entry.ledger):
            return entry.ledger
    return None


def book_holdings(ledger: Ledger, minimum: float = RebalanceRules().min_notional) -> list[str]:
    """What a book holds beyond dust at its last fill prices, read from the ledger alone."""
    held: dict[str, float] = {}
    last: dict[str, float] = {}
    for fill in ledger.fills():
        held[fill.symbol] = held.get(fill.symbol, 0.0) + fill.quantity
        last[fill.symbol] = fill.price
    for adjustment in ledger.adjustments():
        if adjustment.symbol:
            held[adjustment.symbol] = held.get(adjustment.symbol, 0.0) + adjustment.quantity
    return sorted(
        symbol
        for symbol, quantity in held.items()
        if quantity > 0 and quantity * last.get(symbol, float("inf")) >= minimum
    )


def halt_targets(own: Path | None, data_dir: Path = Path("data")) -> list[tuple[str, Path, bool]]:
    """The other sessions a /halt reaches, by name, ledger, and whether they run: those of
    this clone's config/ and those it ran (a config since edited into errors or to another
    ledger). Besides those running, a stopped one is asked when a supervisor may start it
    again (between its restarts, or at the next boot) or its book holds coins: it halts at
    its next start."""
    candidates: dict[Path, str] = {}
    for path in sorted(Path("config").glob("*.y*ml")):
        try:
            raw = load_yaml(path.read_text(encoding="utf-8"))
        except Exception:  # not YAML now: the sessions run know its ledger
            continue
        ledger = named_ledger(raw) if isinstance(raw, dict) and looks_like_session(raw) else None
        if ledger is not None:
            candidates.setdefault(ledger.resolve(), path.stem)
    for entry in registered_sessions(data_dir):
        candidates.setdefault(entry.ledger.resolve(), entry.label)
    found = []
    mine = own.resolve() if own is not None else None
    for ledger, name in candidates.items():
        if ledger == mine or not ledger.is_file():
            continue
        try:
            running = is_running(ledger)
            if not running:
                with Ledger(ledger) as book:
                    if book.get_meta(SUPERVISED_META) != "1" and not book_holdings(book):
                        continue  # started by hand, and nothing to sell
        except Exception:  # a ledger that cannot be read: `tbot halt` it
            continue
        found.append((name, ledger, running))
    return found


def halt_book(config: SessionConfig) -> tuple[bool, str, dict[str, float]]:
    """Halt a session that is not running: whether it was halted already, the reason in
    force, and what the book holds, which its next start sells. Hold instance_lock."""
    ledger = Ledger(config.ledger)
    try:
        guard = load_guard(config, ledger)
        already = guard.state.halted
        if not already:
            guard.halt(HALT_BY_HAND)
            with ledger.atomic():
                ledger.set_meta(GUARD_META, guard.state.model_dump_json())
                ledger.add_event(utc_now(), "warning", f"kill switch: {HALT_BY_HAND}, not running")
        return already, guard.state.halt_reason, held_positions(config, ledger)
    finally:
        ledger.close()


async def watch_stop_request(path: Path, stop: asyncio.Event) -> None:
    while not stop.is_set():
        if path.exists():
            try:
                path.unlink(missing_ok=True)
            except PermissionError:  # Windows: still open in another process; next poll
                await asyncio.sleep(STOP_POLL_SECONDS)
                continue
            log.info("stop_requested")
            stop.set()
            return
        await asyncio.sleep(STOP_POLL_SECONDS)


@contextmanager
def instance_lock(ledger: Path, wait: float = 0.0) -> Iterator[None]:
    """One process per ledger: a second one would trade the same book twice.

    The operating system releases the lock when the process dies, so a crash never
    leaves a stale lock behind. `wait` rides out a brief check by another command.
    """
    path = ledger.with_name(ledger.name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                lock_file(handle, lock=True)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise AlreadyRunning(
                        f"another tbot process is using {ledger.resolve()}; stop it first"
                    ) from None
                time.sleep(0.2)
        try:
            yield
        finally:
            lock_file(handle, lock=False)
    finally:
        handle.close()


@dataclass
class Context:
    """What an executor setup needs from the running session."""

    config: SessionConfig
    settings: Settings
    session: TradingSession
    ledger: Ledger
    notifier: Notifier
    clock: ServerClock
    aclient: httpx.AsyncClient
    keys: list[StreamKey]
    label: str
    lock: asyncio.Lock  # held while the book is read or written
    guard: RiskGuard
    health: Health
    since: datetime  # money found at the start moved at this time (transfer_time)


Setup = Callable[[Context], Awaitable[tuple[Executor, list[Coroutine[Any, Any, None]]]]]
# Checks before the budget is booked: refusing then leaves a changed budget for next time.
Precheck = Callable[[Ledger, httpx.AsyncClient, ServerClock], Awaitable[None]]


async def run_session(
    config: SessionConfig,
    settings: Settings,
    data_dir: Path,
    *,
    label: str,
    setup: Setup,
    stop: asyncio.Event | None = None,
    name: str = "",
    precheck: Precheck | None = None,
    source: Path | None = None,
) -> int:
    """Run until stopped. Returns a process exit code. `name` is the config as typed on
    the command line, for the commands alerts suggest; `source` is its file."""
    new_ledger = not config.ledger.exists()
    with instance_lock(config.ledger, wait=3.0), awake():
        if take_stop_request(config.ledger):
            if supervised():
                log.warning("stopped_at_start", reason="`tbot stop` asked this session to stop")
                return 0
            # Started by hand on purpose: the request was for a supervisor's restart.
            log.warning("stop_request_dropped", reason="this session was started by hand")
        ledger = Ledger(config.ledger)
        try:
            problem = mode_problem(config, ledger)
            if problem:
                raise LedgerModeError(problem)
            ledger.set_meta(MODE_META, session_mode(config))
            ledger.set_meta(SUPERVISED_META, "1" if supervised() else "0")
            ledger.mark_written()
            register_session(data_dir, config.ledger, source, name)
            return await _run_session(
                config,
                settings,
                data_dir,
                ledger,
                label=label,
                setup=setup,
                stop=stop,
                new_ledger=new_ledger,
                name=name,
                precheck=precheck,
            )
        finally:
            ledger.close()


async def _run_session(
    config: SessionConfig,
    settings: Settings,
    data_dir: Path,
    ledger: Ledger,
    *,
    label: str,
    setup: Setup,
    stop: asyncio.Event | None,
    new_ledger: bool,
    name: str,
    precheck: Precheck | None,
) -> int:
    stop = stop or asyncio.Event()
    install_stop_handlers(stop)
    store = BarStore(data_dir)
    keys = stream_keys(config)
    lock = asyncio.Lock()
    health = Health()
    async with httpx.AsyncClient() as aclient:
        direct = make_notifier(settings, aclient)
        notifier = QueuedNotifier(direct)
        sender = asyncio.create_task(notifier.run(), name="alerts")
        try:
            return await _trade(
                config,
                settings,
                ledger,
                store,
                keys,
                lock,
                health,
                aclient,
                direct,
                notifier,
                label=label,
                setup=setup,
                stop=stop,
                new_ledger=new_ledger,
                name=name,
                precheck=precheck,
            )
        finally:
            await notifier.flush(ALERT_FLUSH_SECONDS)
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)


async def _trade(
    config: SessionConfig,
    settings: Settings,
    ledger: Ledger,
    store: BarStore,
    keys: list[StreamKey],
    lock: asyncio.Lock,
    health: Health,
    aclient: httpx.AsyncClient,
    direct: Notifier,
    notifier: Notifier,
    *,
    label: str,
    setup: Setup,
    stop: asyncio.Event,
    new_ledger: bool,
    name: str,
    precheck: Precheck | None,
) -> int:
    with httpx.Client(timeout=30.0) as client:
        try:
            clock = ServerClock(client)
            await asyncio.to_thread(clock.sync)
            now = clock.now()
            log.info("syncing_store", streams=[f"{s}:{tf}" for s, tf in keys])
            lookback = lookback_bars(build_slots(config.strategies), config.risk)
            for symbol, timeframe in keys:
                depth = max(SYNC_LOOKBACK_BARS, lookback.get((symbol, timeframe), 0))
                start = now - timeframe.delta * depth
                closed = now - CLOSE_GRACE  # the clock may run a little ahead
                await asyncio.to_thread(sync, store, client, symbol, timeframe, start, closed)

            if precheck is not None:
                await precheck(ledger, aclient, clock)
            guard = load_guard(config, ledger)
            checkpoint = load_checkpoint(config, ledger)
            since = transfer_time(config, ledger, checkpoint, now)
            moved = check_budget(config, ledger, guard, now, store, since)  # it may adjust it
            portfolio = restore_portfolio(config, ledger)
            session = build_session(config, store, portfolio, now, checkpoint)
            _check_config(config, ledger)
            context = Context(
                config,
                settings,
                session,
                ledger,
                notifier,
                clock,
                aclient,
                keys,
                label,
                lock,
                guard,
                health,
                since,
            )
            executor, extra = await setup(context)
        except Exception as exc:  # tell the operator; a supervisor would restart silently
            log.error("start_failed", error=repr(exc))
            ledger.add_event(utc_now(), "error", f"failed to start: {exc!r}")
            names = settings.key_names(config.mode) if isinstance(config, LiveConfig) else ""
            hint = key_hint(exc, names) if names else ""
            hint = f"; {hint}" if hint else ""
            if permanent(exc):  # the same at every try: say so, and let the supervisor stop
                await notifier.send(
                    f"[{label}] cannot start: {exc}{hint}; fix the config, then start"
                )
                return EXIT_REFUSED
            await notifier.send(f"[{label}] failed to start: {start_problem(exc)}{hint}")
            return 1
        if moved:
            await notifier.send(f"[{label}] {moved}: booked as a transfer, not a profit or loss")
        trader = SessionTrader(
            session,
            ledger,
            executor,
            notifier,
            guard,
            clock.now,
            label,
            lock=lock,
            health=health,
            name=name,
        )

        feed = LiveFeed(
            keys,
            store,
            client,
            # From what the session has seen, so a bar another process stored during
            # the setup above is still handed to this one.
            last={key: session.histories[key].last_open_time for key in keys},
            clock=clock.now,
            stale_after=config.stale_after_seconds,
            batch_wait=config.batch_wait_seconds,
            on_stale=lambda key, last: _report_stale(ledger, notifier, label, key, last),
            on_fresh=lambda key: _report_fresh(ledger, notifier, label, key),
            on_alert=lambda text: _report_feed(ledger, notifier, label, text),
        )
        trader.quiet = lambda: quiet_streams(feed, getattr(executor, "spot", None))
        if isinstance(direct, Telegram):
            direct.on_refused = lambda problem: ledger.add_event(
                utc_now(), "error", f"Telegram refuses alerts: {problem}; `tbot notify` tests it"
            )
        point = session.snapshot(now)
        where = config.ledger.resolve()
        ledger.add_event(now, "info", f"started ({label}), tbot {__version__}, ledger {where}")
        halted = f", HALTED: {guard.state.halt_reason}" if guard.state.halted else ""
        fresh = " (new)" if new_ledger else ""
        budget = ""
        if isinstance(config, LiveConfig) and config.mode == "live":
            budget = (
                f"REAL MONEY, budget {config.initial_cash:,.2f} (ownership {config.ownership}), "
            )
        await notifier.send(
            f"[{label}] started (tbot {__version__}): {budget}{len(keys)} streams, "
            f"equity {point.equity:,.2f}, "
            f"positions {len(portfolio.positions)}, fills so far {len(portfolio.fills)}, "
            f"ledger {where}{fresh}{halted}"
        )
        await trader.sell_if_halted()

        release: list[str] = []  # an update note, once a newer release is found

        def notes() -> list[str]:
            found = [f"PAUSED: {health.blocked}"] if health.blocked else []
            found.extend(f"UPDATE: {note}" for note in release)
            unprotected = sorted(getattr(executor, "unprotected", ()))
            if unprotected:
                found.append(f"NO EXCHANGE STOP: {', '.join(unprotected)}")
            partial = sorted(getattr(executor, "partial", {}).items())
            if partial:
                found.append(
                    "PART WITHOUT A STOP: "
                    + ", ".join(f"{symbol} {qty:.6f}" for symbol, qty in partial)
                )
            disk = low_disk(drives)
            if disk:
                found.append(f"LOW DISK: {disk}")
            found.extend(clock_note(clock.offset.total_seconds()))
            return found

        drives = [config.ledger.parent, store.base]

        hour = config.summary_hour_utc
        resume = resume_command(name)
        summary = summary_loop(session, ledger, notifier, hour, label, notes, store, resume)
        tasks = [
            asyncio.create_task(clock_loop(clock, CLOCK_RESYNC_SECONDS), name="clock"),
            asyncio.create_task(feed.run_websocket(), name="websocket"),
            asyncio.create_task(feed.run_watchdog(), name="watchdog"),
            asyncio.create_task(consume(feed, trader), name="consume"),
            asyncio.create_task(summary, name="summary"),
            asyncio.create_task(release_loop(release), name="release"),
            *(asyncio.create_task(coro, name=f"extra{i}") for i, coro in enumerate(extra)),
            asyncio.create_task(
                watch_stop_request(stop_path(config.ledger), stop), name="stop-request"
            ),
            asyncio.create_task(
                watch_halt_request(halt_path(config.ledger), trader.halt_now), name="halt-request"
            ),
            asyncio.create_task(halt_retry_loop(trader), name="halt-retry"),
            asyncio.create_task(disk_loop(drives, ledger, notifier, label), name="disk"),
        ]
        if isinstance(direct, Telegram):
            tasks.append(asyncio.create_task(refusal_probe_loop(direct), name="telegram-probe"))
        if config.backup_days:
            backups = backup_loop(
                ledger, config.ledger, config.backup_days, lock, notifier, label, clock.now
            )
            tasks.append(asyncio.create_task(backups, name="backup"))
        if config.telegram_commands and isinstance(direct, Telegram):
            answer = command_answer(
                session,
                ledger,
                label,
                clock.now,
                notes,
                store,
                trader.halt_now,
                name,
                lambda: halt_targets(config.ledger, store.base),
            )
            real_money = isinstance(config, LiveConfig) and config.mode == "live"
            turn = PollTurn(settings.telegram_token or "", RANK_REAL_MONEY if real_money else 0)
            commands = command_loop(direct, answer, clock.now(), turn, label, notifier.send)
            tasks.append(asyncio.create_task(commands, name="commands"))
        elif config.telegram_commands and (settings.telegram_token or settings.telegram_chat_id):
            log.warning("telegram_commands_off", hint="set both TBOT_TELEGRAM_TOKEN and _CHAT_ID")
        if settings.heartbeat_url and not valid_url(settings.heartbeat_url):
            log.warning("heartbeat_url_invalid", hint=HEARTBEAT_URL_HINT)
        elif settings.heartbeat_url:
            spot = getattr(executor, "spot", None)

            def problem() -> str:
                refused = direct.refused if isinstance(direct, Telegram) else ""
                stuck = notifier.undelivered() if isinstance(notifier, QueuedNotifier) else 0.0
                return heartbeat_problem(feed.stale(), health.blocked, spot, refused, stuck)

            url, every = settings.heartbeat_url, config.heartbeat_seconds
            beat = heartbeat_loop(url, every, aclient, problem)
            tasks.append(asyncio.create_task(beat, name="heartbeat"))
        stopper = asyncio.create_task(stop.wait(), name="stop")
        done, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
        code = 0
        for task in done:
            if task is not stopper and task.exception() is not None:
                error = repr(task.exception())
                log.error("task_crashed", task=task.get_name(), error=error)
                ledger.add_event(utc_now(), "error", f"{task.get_name()} crashed: {error}")
                await notifier.send(f"[{label}] CRASHED in {task.get_name()}: {error}")
                code = 1
        # An event in progress finishes its orders and ledger rows before anything stops.
        try:
            await asyncio.wait_for(lock.acquire(), SHUTDOWN_GRACE_SECONDS)
            held = True
        except TimeoutError:
            held = False
            log.error("shutdown_during_event", hint="the next start settles open orders")
        for task in [*tasks, stopper]:
            task.cancel()
        await asyncio.gather(*tasks, stopper, return_exceptions=True)
        if held:
            lock.release()
        unguarded = await final_protect(executor, session.portfolio, session.marks)
        ledger.add_event(utc_now(), "info", "stopped")
        equity = session.portfolio.equity(session.marks)
        holding = held_text(session.portfolio, session.marks, config.rebalance.min_notional)
        guarded = isinstance(executor, LiveExecutor) and executor.protective_stop_pct > 0
        if unguarded:
            holding += f"; NO EXCHANGE STOP: {', '.join(unguarded)} until the next start"
        elif guarded and trader.sellable():
            holding += " (their exchange stops stay; after `tbot halt` a start sells them)"
        await notifier.send(f"[{label}] stopped, equity {equity:,.2f}, {holding}")
    return code


async def final_protect(
    executor: Executor, portfolio: Portfolio, marks: Mapping[str, float]
) -> list[str]:
    """Place the stops an event cut short at shutdown left out (cancelled, not yet replaced),
    within FINAL_PROTECT_SECONDS; return the held symbols still without one."""
    if not isinstance(executor, LiveExecutor) or not executor.protective_stop_pct:
        return []
    held = {symbol for symbol, quantity in portfolio.positions.items() if quantity > 0}
    executor.unprotected |= executor.stopless & held
    if executor.unprotected & held:
        try:
            protect = executor.protect(portfolio, marks)
            await asyncio.wait_for(protect, FINAL_PROTECT_SECONDS)
        except Exception as exc:  # a timeout too: the stopped alert names what is left
            log.error("final_protect_failed", error=repr(exc))
    return sorted((executor.unprotected | executor.stopless) & held)


def quiet_streams(feed: LiveFeed, spot: BinanceSpot | None) -> set[StreamKey]:
    """Streams a close may never bring: stale ones, and pairs Binance does not trade."""
    found = set(feed.stale())
    if spot is not None:
        found |= {key for key in feed.keys if not spot.trading(key[0])}
    return found


def heartbeat_problem(
    stale: Sequence[StreamKey],
    blocked: str,
    spot: BinanceSpot | None = None,
    alerts: str = "",
    undelivered: float = 0.0,
) -> str:
    """Why the heartbeat should not ping, or "": a stale stream, paused orders, alerts
    Telegram refuses, or one that has not got through for a while (the monitor is then the
    only way to hear of trouble). A pair Binance pauses may send no bars while the rest
    trades on, so it does not count."""
    named = ", ".join(
        f"{symbol} {timeframe}"
        for symbol, timeframe in stale
        if spot is None or spot.trading(symbol)
    )
    if named:
        return f"stale streams: {named}"
    if blocked:
        return blocked
    if alerts:
        return f"Telegram refuses alerts ({alerts})"
    if undelivered > ALERTS_STUCK_SECONDS:
        return f"alerts not delivered for {undelivered / 60:.0f} minutes"
    return ""


async def refusal_probe_loop(telegram: Telegram) -> None:
    """While Telegram refuses alerts, ask now and then whether it takes them again (the
    owner unblocked the bot), so the heartbeat need not wait for the next alert."""
    while True:
        await asyncio.sleep(REFUSAL_PROBE_SECONDS)
        if telegram.refused:
            await telegram.probe()


def clock_note(offset: float) -> list[str]:
    """A summary note on a PC clock far off the exchange's (offset: server minus local)."""
    if abs(offset) <= CLOCK_NOTE_SECONDS:
        return []
    way = "behind" if offset > 0 else "ahead of"
    return [
        f"CLOCK: this PC's clock is {abs(offset):.0f} s {way} the exchange's; trading "
        "corrects it, log times do not: let the system set the time"
    ]


def low_disk(paths: Sequence[Path]) -> str:
    """A drive the bot writes to with little room left, or ""."""
    for path in paths:
        try:
            free = shutil.disk_usage(path).free
        except OSError:
            continue
        if free < LOW_DISK_BYTES:
            return f"{free / 1024**3:.1f} GB free on the drive of {path.resolve()}"
    return ""


async def disk_loop(paths: Sequence[Path], ledger: Ledger, notifier: Notifier, label: str) -> None:
    """Say once when a drive runs low, before a full disk stops the bot."""
    said = False
    while True:
        problem = await asyncio.to_thread(low_disk, paths)
        if problem and not said:
            text = f"LOW DISK: {problem}; a full disk stops the bot: make room"
            ledger.add_event(utc_now(), "warning", text)
            await notifier.send(f"[{label}] {text}")
        said = bool(problem)  # said again after there was room once more
        await asyncio.sleep(DISK_CHECK_SECONDS)


@contextmanager
def awake() -> Iterator[None]:
    """Keep Windows from going to sleep while a session runs: asleep, it trades nothing and
    refits no stop. Closing a lid or the sleep button still puts it to sleep."""
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    try:
        yield
    finally:
        if sys.platform == "win32":
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)


def valid_url(url: str) -> bool:
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        parsed = None
    return parsed is not None and parsed.scheme in ("http", "https") and bool(parsed.host)


async def run_paper(
    config: PaperConfig,
    settings: Settings,
    data_dir: Path,
    *,
    stop: asyncio.Event | None = None,
    name: str = "",
    source: Path | None = None,
) -> int:
    label = session_label("paper", name)

    async def setup(ctx: Context) -> tuple[Executor, list[Coroutine[Any, Any, None]]]:
        executor = PaperExecutor(
            SimulatedBroker(config.costs),
            BinanceBookTicker(ctx.aclient),
            ctx.ledger,
            ctx.notifier,
            ctx.clock.now,
            label=label,
        )
        return executor, []

    return await run_session(
        config, settings, data_dir, label=label, setup=setup, stop=stop, name=name, source=source
    )


def make_spot(
    config: LiveConfig,
    settings: Settings,
    client: httpx.AsyncClient,
    clock: Callable[[], datetime],
    resync: Callable[[], Awaitable[object]] | None = None,
) -> BinanceSpot:
    keys = settings.api_keys(config.mode)
    if keys is None:
        raise StartRefused(KEYS_MISSING)
    return BinanceSpot(
        client,
        *keys,
        base_url=TESTNET_URL if config.mode == "testnet" else PRODUCTION_URL,
        clock=clock,
        recv_window=config.recv_window,
        resync=resync,
    )


async def reconcile_round(
    executor: LiveExecutor,
    portfolio: Portfolio,
    marks: Mapping[str, float],
    ledger: Ledger,
    notifier: Notifier,
    guard: RiskGuard,
    config: LiveConfig,
    clock: Callable[[], datetime],
) -> None:
    """Book what the exchange did between events, compare the rest, and fit the stops.

    Whatever settle books (an order in doubt, an executed stop) changes the book, so the
    stops are replaced to fit it, as after an event.
    """
    spot = executor.spot
    await executor.check_rules()
    booked = await executor.settle(portfolio)
    balances = await spot.balances()
    late = await executor.settle(portfolio)
    apply_budget_shift(guard, ledger, portfolio.equity(marks))
    if late or executor.in_doubt():
        # Something executed while the balances were read (they may show it or not), or
        # an order Binance may still list: comparing now would take a trade for a
        # transfer and book it twice. Next round.
        log.info("reconcile_deferred", in_doubt=not late)
        if booked or late:
            await executor.after_event(portfolio, marks)
        elif executor.unprotected:
            await executor.protect(portfolio, marks)
        return

    def shift_guard(adjustments: list[Adjustment]) -> None:
        # Money moved from outside: not a profit or loss for the guard.
        moved = sum(a.cash + a.quantity * marks.get(a.symbol, 0.0) for a in adjustments)
        adopt_resume(guard, ledger)
        equity = portfolio.equity(marks)  # the book is adjusted after this
        guard.shift(moved, equity)
        ledger.set_meta(GUARD_META, guard.state.model_dump_json())
        note_transfer(ledger, equity + moved)

    adjustments = await reconcile(
        spot,
        portfolio,
        symbols_of(config),
        ledger,
        notifier,
        clock(),
        tolerance=config.reconcile_tolerance,
        ownership=config.ownership,
        label=executor.label,
        balances=balances,
        on_adjust=shift_guard,
        cash_floor=budget_owed(ledger),
    )
    note_repaid(ledger, portfolio.cash)
    if adjustments or booked:
        await executor.after_event(portfolio, marks)
    elif executor.unprotected:
        await executor.protect(portfolio, marks)


async def reconcile_loop(
    round_: Callable[[], Awaitable[None]],
    interval: float,
    lock: asyncio.Lock,
    health: Health,
    ledger: Ledger,
    notifier: Notifier,
    label: str,
) -> None:
    """Reconcile every interval; after three failures in a row new buys wait."""
    failures = 0
    while True:
        await asyncio.sleep(interval)
        try:
            async with lock:  # never while an order is in flight
                await round_()
        except Exception as exc:  # any failure: count it, keep the loop alive
            failures += 1
            log.warning("reconcile_failed", failures=failures, error=repr(exc))
            if failures == RECONCILE_ALERT_AFTER:
                reason = f"reconciliation failing ({exc})"
                health.blocked = reason
                ledger.add_event(utc_now(), "error", reason)
                await notifier.send(
                    f"[{label}] {reason}; new buys wait until it works again, sales still go out"
                )
            continue
        if failures >= RECONCILE_ALERT_AFTER:
            health.blocked = ""
            ledger.add_event(utc_now(), "info", "reconciliation works again")
            await notifier.send(f"[{label}] reconciliation works again")
        failures = 0


async def run_live(
    config: LiveConfig,
    settings: Settings,
    data_dir: Path,
    *,
    confirmed: bool = False,
    stop: asyncio.Event | None = None,
    name: str = "",
    source: Path | None = None,
) -> int:
    if config.mode == "live" and not confirmed:
        raise ValueError("config mode is live: pass --live to trade real money")
    if settings.api_keys(config.mode) is None:
        raise StartRefused(KEYS_MISSING)  # before a ledger exists for nothing
    if config.mode == "live" and not (settings.telegram_token and settings.telegram_chat_id):
        raise StartRefused(
            "a real-money session must be able to send alerts: set up Telegram with "
            "`tbot notify` first"
        )
    label = session_label(config.mode, name)

    async def precheck(ledger: Ledger, client: httpx.AsyncClient, clock: ServerClock) -> None:
        if config.mode == "live":
            await check_telegram(settings, client)
        await check_funds(config, settings, ledger, client, clock)

    async def setup(ctx: Context) -> tuple[Executor, list[Coroutine[Any, Any, None]]]:
        async def resync() -> None:
            await asyncio.to_thread(ctx.clock.sync)

        spot = make_spot(config, settings, ctx.aclient, ctx.clock.now, resync)
        problem = quote_problem(config, await spot.load_rules(symbols_of(config)))
        if problem:
            raise StartRefused(problem)
        if config.mode == "live":
            for note in await check_key_rights(spot):
                await ctx.notifier.send(f"[{label}] {note}")
        executor = LiveExecutor(
            spot,
            ctx.ledger,
            ctx.notifier,
            ctx.clock.now,
            fee_rate=config.costs.fee_rate,
            protective_stop_pct=config.protective_stop_pct,
            label=label,
        )
        portfolio = ctx.session.portfolio

        async def reconcile_once(clock: Callable[[], datetime] = ctx.clock.now) -> None:
            await reconcile_round(
                executor,
                portfolio,
                ctx.session.marks,
                ctx.ledger,
                ctx.notifier,
                ctx.guard,
                config,
                clock,
            )

        async with ctx.lock:
            # A key Binance rejects for good ends the start (permanent). Money moved while
            # the bot was down is booked before any bar the start hands over.
            await reconcile_once(lambda: ctx.since)
            await executor.after_event(portfolio, ctx.session.marks)  # stops for held positions

        loop = reconcile_loop(
            reconcile_once,
            config.reconcile_seconds,
            ctx.lock,
            ctx.health,
            ctx.ledger,
            ctx.notifier,
            label,
        )
        return executor, [loop]

    return await run_session(
        config,
        settings,
        data_dir,
        label=label,
        setup=setup,
        stop=stop,
        name=name,
        precheck=precheck,
        source=source,
    )


# Rights a trading key may have that tbot never uses: each lets a thief do more with it.
NEEDLESS_RIGHTS = {
    "enableMargin": "margin trading",
    "enableFutures": "futures trading",
    "enableVanillaOptions": "options trading",
    "enableInternalTransfer": "internal transfers",
    "permitsUniversalTransfer": "transfers between wallets",
}


async def check_key_rights(spot: BinanceSpot) -> list[str]:
    """Real money only with a key that cannot withdraw: a stolen one could empty the
    account. Returns what the owner should hear: rights Binance does not show (the start
    goes on; doctor asks again) and rights the bot does not need."""
    try:
        rights = await spot.api_restrictions()
    except (httpx.HTTPError, BinanceError) as exc:
        log.warning("key_rights_unknown", error=repr(exc))
        return [
            f"could not read the API key's permissions ({exc}); `tbot doctor` checks that "
            "withdrawals are off"
        ]
    if rights.get("enableWithdrawals", True):
        raise StartRefused(
            "the API key can withdraw: turn off withdrawals for it in Binance API Management"
        )
    if not rights.get("enableSpotAndMarginTrading", False):
        raise StartRefused("the API key cannot trade spot: turn on spot trading for it")
    extra = needless_rights(rights)
    return [f"the API key also allows {extra}, which tbot never uses: turn it off"] if extra else []


def needless_rights(rights: Mapping[str, Any]) -> str:
    return ", ".join(text for key, text in NEEDLESS_RIGHTS.items() if rights.get(key))


async def check_telegram(settings: Settings, client: httpx.AsyncClient) -> None:
    """A real-money session starts only when Telegram takes its alerts: it knows the token,
    and the bot can reach the chat. An outage only delays them, so it does not stop a start."""
    bot = Telegram(settings.telegram_token or "", settings.telegram_chat_id or "", client)
    step = "token"
    try:
        await bot.me()
        step = "chat"
        await bot.chat()
        await bot.typing()  # getChat still answers for a bot the owner blocked
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status >= 500 or status == 429 or migrated_to(exc.response):
            log.warning("telegram_check_failed", error=describe_error(exc))  # it retries
            return
        if step == "token":
            raise StartRefused(
                "Telegram does not know TBOT_TELEGRAM_TOKEN, so no alert would arrive: "
                "run `tbot notify`"
            ) from None
        raise StartRefused(
            "the bot cannot send to TBOT_TELEGRAM_CHAT_ID (a wrong id, or the bot was removed "
            "from the chat or blocked), so no alert would arrive: run `tbot notify --chat`"
        ) from None
    except httpx.HTTPError as exc:
        log.warning("telegram_check_failed", error=describe_error(exc))


async def check_funds(
    config: LiveConfig,
    settings: Settings,
    ledger: Ledger,
    client: httpx.AsyncClient,
    clock: ServerClock,
) -> None:
    """With budget ownership the account must hold the budget in free cash when the book
    first takes it, and when a raise adds to it: else the first reconciliation shrinks the
    book to what is there, and a mistyped budget goes unnoticed."""
    if config.ownership != "budget":
        return
    stored = ledger.get_meta(BUDGET_META)
    raised = config.initial_cash - (float(stored) if stored is not None else 0.0)
    if raised <= 0:
        return
    if ledger.unresolved_orders():  # the book's cash is known once they are settled
        log.warning("budget_check_skipped", reason="orders in doubt")
        return
    needed = restore_portfolio(config, ledger).cash + (raised if stored is not None else 0.0)
    spot = make_spot(config, settings, client, clock.now)
    rules = await spot.load_rules(symbols_of(config))
    problem = quote_problem(config, rules)
    if problem:
        raise StartRefused(problem)
    quote = next(iter(rules.values())).quote
    balances = await spot.balances()
    free = balances[quote].free if quote in balances else 0.0
    taken, books = other_books_cash(config)
    if free - taken + 1e-9 < needed:
        theirs = f", {taken:,.2f} of it the cash of {', '.join(books)}" if books else ""
        raise StartRefused(
            f"the account has {quote} {free:,.2f} free{theirs}, and the bot's budget needs "
            f"{needed:,.2f}: check initial_cash, then lower it or deposit"
        )


def ledger_cash(ledger: Ledger) -> float | None:
    """A book's cash from its ledger alone (its config may no longer load); None before its
    first start."""
    start = ledger.get_meta(BASE_META) or ledger.get_meta(BUDGET_META)
    if start is None:
        return None
    spent = sum(fill.quantity * fill.price + fill.fee for fill in ledger.fills())
    return float(start) - spent + sum(adjustment.cash for adjustment in ledger.adjustments())


def quote_problem(config: SessionConfig, rules: Mapping[str, SymbolRules]) -> str:
    """Why the book cannot trade these symbols, or "": it keeps one cash, and its amounts
    count in that quote asset (an order minimum of 10 is 10 BTC on ETHBTC)."""
    quotes = sorted({r.quote for r in rules.values()})
    if len(quotes) > 1:
        return f"the symbols are quoted in {', '.join(quotes)}: {ONE_QUOTE}"
    largest = max((float(r.min_notional) for r in rules.values()), default=0.0)
    minimum = config.rebalance.min_notional
    if largest and minimum > largest * MINIMUM_FACTOR:
        return (
            f"rebalance.min_notional {minimum:g} counts in {quotes[0]}, the quote asset, and is "
            f"{minimum / largest:,.0f} times Binance's minimum of {largest:g}: no smaller order "
            f"is sent; set it near Binance's minimum"
        )
    return ""


def other_books_cash(config: LiveConfig) -> tuple[float, list[str]]:
    """The cash other budget books of this clone's config/ keep on the same account (the
    same mode, so the same keys): money a new budget must not count on."""
    total, names, seen = 0.0, [], {config.ledger.resolve()}
    for path in sorted(Path("config").glob("*.y*ml")):
        try:
            raw = load_yaml(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("mode") != config.mode:
                continue
            # A config edited into errors still has a book that keeps its cash.
            ledger, ownership = named_ledger(raw), raw.get("ownership", "budget")
            if ledger is None or ownership != "budget" or not ledger.is_file():
                continue
            if ledger.resolve() in seen:
                continue
            seen.add(ledger.resolve())
            with Ledger(ledger) as book:
                cash = ledger_cash(book)
        except Exception:  # a file or ledger that cannot be read keeps nothing known
            continue
        if cash is not None and cash > 0:
            total += cash
            names.append(path.stem)
    return total, names


def resume(config: SessionConfig) -> str:
    """Clear the kill switch. Returns a message for the operator."""
    ledger = Ledger(config.ledger)
    try:
        guard = load_guard(config, ledger)
        if not guard.state.halted:
            request = halt_path(config.ledger)
            if not request.exists():
                return "not halted"
            request.unlink(missing_ok=True)  # it would sell at the next start
            ledger.add_event(utc_now(), "info", "a `tbot halt` request no session took withdrawn")
            return "not halted; withdrew a `tbot halt` request no session had taken"
        reason = guard.state.halt_reason
        guard.resume()
        ledger.set_meta(GUARD_META, guard.state.model_dump_json())
        ledger.add_event(utc_now(), "info", f"resumed after halt: {reason}")
        halt_path(config.ledger).unlink(missing_ok=True)  # one no session took: done with
        return f"resumed (was halted: {reason}); a running bot trades again from its next bar"
    finally:
        ledger.close()


async def account_text(config: LiveConfig, settings: Settings) -> str:
    with httpx.Client(timeout=30.0) as sync_client:
        clock = ServerClock(sync_client)  # signed requests need the exchange's time
        await asyncio.to_thread(clock.sync)
    async with httpx.AsyncClient() as client:
        spot = make_spot(config, settings, client, clock.now)
        symbols = symbols_of(config)
        rules = await spot.load_rules(symbols)
        balances = await spot.balances()
        assets = sorted({r.base for r in rules.values()} | {r.quote for r in rules.values()})
        lines = [f"{config.mode} account at {spot.base_url}"]
        for asset in assets:
            balance = balances.get(asset)
            free, locked = (balance.free, balance.locked) if balance else (0.0, 0.0)
            lines.append(f"{asset}: free {free:.8f}, locked {locked:.8f}")
        for symbol in symbols:
            for order in await spot.open_orders(symbol):
                lines.append(
                    f"open {symbol} {order.type} {order.side} {order.orig_qty:.6f} "
                    f"stop {order.stop_price} id {order.client_order_id}"
                )
        return "\n".join(lines)


def _check_config(config: SessionConfig, ledger: Ledger) -> None:
    """Warn when the ledger was started under other trading settings: what a backtest runs,
    not the budget, the summary hour, or other operational settings."""
    current = config_hash(config.model_dump_json(include=HASHED))
    stored = ledger.get_meta("config_hash")
    if stored is None:
        ledger.set_meta("config_hash", current)
        ledger.set_meta("created_at", utc_now().isoformat())
    elif stored == config_hash(config.model_dump_json(exclude=OPERATIONAL)):
        ledger.set_meta("config_hash", current)  # hashed by an older tbot: nothing changed
    elif stored != current:
        log.warning("config_changed", stored=stored, current=current)
        ledger.add_event(utc_now(), "warning", f"config changed: {stored} -> {current}")
        ledger.set_meta("config_hash", current)


def _stamp(iso: str | None) -> str:
    return f"{iso[:16].replace('T', ' ')} UTC" if iso else "-"


def _report_stale(
    ledger: Ledger, notifier: Notifier, label: str, key: StreamKey, last: datetime
) -> None:
    symbol, timeframe = key
    seen = (  # last is the bar's open time
        f"last bar closed {last + timeframe.delta:%Y-%m-%d %H:%M} UTC"
        if last.year > 1
        else "no bar yet"
    )
    message = f"[{label}] stale stream {symbol} {timeframe}: {seen}"
    log.warning("stale_stream", symbol=symbol, timeframe=str(timeframe), last=last.isoformat())
    ledger.add_event(utc_now(), "warning", message)
    _send_later(notifier, message)


def _report_fresh(ledger: Ledger, notifier: Notifier, label: str, key: StreamKey) -> None:
    symbol, timeframe = key
    message = f"[{label}] stream {symbol} {timeframe} is back: bars arrive again"
    log.warning("stream_back", symbol=symbol, timeframe=str(timeframe))
    ledger.add_event(utc_now(), "info", message)
    _send_later(notifier, message)


def _report_feed(ledger: Ledger, notifier: Notifier, label: str, text: str) -> None:
    message = f"[{label}] {text}"
    ledger.add_event(utc_now(), "warning", message)
    _send_later(notifier, message)


def _send_later(notifier: Notifier, message: str) -> None:
    """Send from code that cannot wait, such as a feed callback."""
    task = asyncio.get_running_loop().create_task(notifier.send(message))
    _background.add(task)  # a bare task can be collected before it runs
    task.add_done_callback(_background.discard)


def status_text(config: SessionConfig, store: BarStore, name: str = "") -> str:
    ledger = Ledger(config.ledger)
    try:
        portfolio = restore_portfolio(config, ledger)
        guard = load_guard(config, ledger)
        point = ledger.latest_equity()
        running = "a session is running" if is_running(config.ledger) else "no session running"
        timeframe, first = next_close(config, utc_now())
        lines = [
            f"ledger {config.ledger.resolve()} ({running})",
            f"created {_stamp(ledger.get_meta('created_at'))}",
            (
                f"equity {point.equity:,.2f} at {point.time:%Y-%m-%d %H:%M} UTC, "
                f"cash {point.cash:,.2f}, exposure {point.exposure:.1%}"
                if point
                else f"no bar event yet: the first comes with the next {timeframe} close, "
                f"{clock_text(first)}"
            ),
            f"fills {len(portfolio.fills)}, round trips {len(portfolio.trades)}, "
            f"adjustments {len(ledger.adjustments())}",
        ]
        unresolved = ledger.unresolved_orders()
        if unresolved:
            lines.append(f"orders in doubt {len(unresolved)} (settled when the bot runs)")
        if guard.state.halted:
            lines.append(f"HALTED: {guard.state.halt_reason} (run `{resume_command(name)}`)")
        if halt_path(config.ledger).exists():
            lines.append(
                "a `tbot halt` request waits: the session sells and halts when it takes it "
                "(`tbot resume` withdraws it)"
            )
        for symbol, qty in sorted(portfolio.positions.items()):
            lines.append(f"position {symbol} {qty:.6f}")
        for symbol, timeframe in stream_keys(config):
            last = store.last_open_time(symbol, timeframe)
            lines.append(
                f"last closed bar {symbol} {timeframe}: {last + timeframe.delta:%Y-%m-%d %H:%M} UTC"
                if last
                else f"no bars {symbol} {timeframe}"
            )
        backups = daily_backups(config.ledger)
        if backups:
            made = datetime.fromtimestamp(backups[-1].stat().st_mtime, UTC)
            lines.append(f"last backup {backups[-1]} at {made:%Y-%m-%d %H:%M} UTC")
        for fill in ledger.recent_fills(5):
            side = "BUY" if fill.quantity > 0 else "SELL"
            lines.append(
                f"fill {fill.time:%Y-%m-%d %H:%M} {side} {abs(fill.quantity):.6f} "
                f"{fill.symbol} @ {price_text(fill.price)}"
            )
        for event in ledger.recent_events(5):
            lines.append(f"event {event.time:%Y-%m-%d %H:%M} {event.level}: {event.message}")
        return "\n".join(lines)
    finally:
        ledger.close()
