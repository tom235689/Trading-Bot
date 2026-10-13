"""Paper, testnet, and live sessions: one event loop, different executors."""

import asyncio
import contextlib
import json
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import IO, Any

import httpx
import polars as pl
import structlog

from tbot import __version__
from tbot.backtest.runner import WARMUP_MARGIN, history_bars
from tbot.core.config import TradingConfig
from tbot.core.models import Fill
from tbot.core.text import clock_text, price_text
from tbot.core.timeframe import Timeframe, to_millis
from tbot.data.downloader import NotListed, sync
from tbot.data.store import BarStore
from tbot.exchange.binance import (
    INVALID_SYMBOL,
    KEY_ERRORS,
    PRODUCTION_URL,
    TESTNET_URL,
    BinanceError,
    BinanceSpot,
)
from tbot.execution.sim_broker import SimulatedBroker
from tbot.live.backup import backup_loop, daily_backups
from tbot.live.clock import CLOSE_GRACE, ServerClock
from tbot.live.commands import HELP, Answer, command_loop
from tbot.live.config import LiveConfig, PaperConfig, SessionConfig, Settings
from tbot.live.executor import BinanceBookTicker, Executor, LiveExecutor, PaperExecutor
from tbot.live.feed import Batch, LiveFeed, bars_since, close_time, utc_now
from tbot.live.history import MAX_BARS, BarHistory
from tbot.live.ledger import Adjustment, Ledger, config_hash
from tbot.live.reconcile import reconcile
from tbot.live.release import newest_release, update_note
from tbot.live.session import Checkpoint, StreamKey, TradingSession, replay_history
from tbot.monitoring.heartbeat import heartbeat_loop
from tbot.monitoring.telegram import LogNotifier, Notifier, QueuedNotifier, Telegram
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
GUARD_META = "guard"
BUDGET_META = "initial_cash"  # the budget the guard levels refer to
BASE_META = "base_cash"  # what the book started with
SUPERVISED_ENV = "TBOT_SUPERVISED"  # set by scripts/run_bot.ps1
BUDGET_NOTE = "initial_cash changed"  # adjustments that book a changed budget
OWED_META = "budget_owed"  # what a lowered budget took beyond the book's cash (<= 0)
# The book's value after the last transfer, with the equity snapshot it follows.
MOVED_META = "equity_after_transfer"
# A live budget change the guard is rescaled for once the first reconciliation has booked
# what traded meanwhile (stops, orders in doubt).
SHIFT_META = "budget_shift_pending"
HEARTBEAT_URL_HINT = "TBOT_HEARTBEAT_URL must be a full http(s):// URL"
MODE_META = "mode"  # paper, testnet, or live: what the book is
EXIT_REFUSED = 4  # as the command line's config error: the supervisor gives up
KEYS_MISSING = (
    "set TBOT_BINANCE_API_KEY and TBOT_BINANCE_API_SECRET in .env: testnet keys from "
    "testnet.binance.vision (or TBOT_BINANCE_TESTNET_API_KEY and _SECRET), live keys from "
    "API Management on binance.com"
)
RELEASE_FIRST = 600.0  # seconds after a start before asking whether a newer release is out
RELEASE_EVERY = 86400.0
KEY_REJECTED = (
    "check TBOT_BINANCE_API_KEY and TBOT_BINANCE_API_SECRET in .env; tbot signs with HMAC, so "
    "the key must be a system-generated (HMAC) one, not Ed25519 or RSA"
)
KEY_REFUSED = -2015  # wrong key, wrong network, missing permission, or IP not allowed
KEY_REFUSED_HINT = (
    "the key is mistyped, made for the other network (testnet keys work only on testnet), "
    "lacks spot trading permission, or allows only other IP addresses"
)


def key_hint(exc: Exception) -> str:
    """What to check when Binance refuses the key, or ""."""
    code = getattr(exc, "code", None)
    if code in KEY_ERRORS:
        return KEY_REJECTED
    return KEY_REFUSED_HINT if code == KEY_REFUSED else ""


TARGETS_META = "targets"
OPERATIONAL = {"ledger", "backup_days", "telegram_commands"}  # not in the config hash
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
        self._paused = False  # a blocked bar was announced; later ones are only logged
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
        decision = self.guard.check(self.clock(), session.portfolio.equity(session.marks), now)
        if decision.mode != Mode.HALT and self.health.blocked:
            decision = Decision(Mode.BLOCK, self.health.blocked)
        self.save_guard()
        fills: list[Fill] = []
        if decision.mode == Mode.HALT:
            fills = await self._halt(decision.reason, now, announce=not was_halted)
        elif decision.mode == Mode.BLOCK:
            self.ledger.add_event(now, "warning", f"blocked: {decision.reason}")
            if not self._paused:
                await self.notifier.send(
                    f"[{self.label}] no trading this bar: {decision.reason}. "
                    f"Further blocked bars are logged, not sent."
                )
            self._paused = True
        else:
            if self._paused:
                self._paused = False
                await self.notifier.send(f"[{self.label}] trading again")
            allowed = self.guard.filter_orders(orders, session.portfolio.positions, decision.mode)
            if decision.mode == Mode.REDUCE_ONLY and allowed != dict(orders):
                self.ledger.add_event(now, "warning", f"reduce only: {decision.reason}")
                await self.notifier.send(f"[{self.label}] entries blocked: {decision.reason}")
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
        orders = self.guard.flatten_orders(session.portfolio.positions)
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
                f"fills{rest}). Trading stays halted until `tbot resume`."
            )
        return fills

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


def check_budget(config: SessionConfig, ledger: Ledger, guard: RiskGuard, now: datetime) -> None:
    """A changed initial_cash moves money into or out of the book: not a profit or loss.

    It is booked as a cash adjustment, like a deposit, so the guard, the dashboard, and
    compare all see a transfer. Run it before the book is restored.
    """
    stored = ledger.get_meta(BUDGET_META)
    with ledger.atomic():
        if ledger.get_meta(BASE_META) is None:
            ledger.set_meta(BASE_META, stored if stored is not None else repr(config.initial_cash))
        if stored is not None and float(stored) != config.initial_cash:
            change = config.initial_cash - float(stored)
            equity = book_equity(config, ledger) + pending_shift(ledger)
            if equity + change < 0:
                raise StartRefused(
                    f"initial_cash {config.initial_cash:,.2f} takes {-change:,.2f} out of a "
                    f"book worth {equity:,.2f}; the lowest it can be now is "
                    f"{float(stored) - equity:,.2f}"
                )
            # What a cut may leave owing is known once orders in doubt are settled, at the
            # first reconciliation; until then the whole cut counts (note_repaid).
            owed = min(0.0, budget_owed(ledger) + change)
            note = f"{BUDGET_NOTE} from {float(stored):,.2f} to {config.initial_cash:,.2f}"
            ledger.add_adjustment(Adjustment(now, "", 0.0, change, note))
            ledger.set_meta(OWED_META, repr(owed))
            if isinstance(config, LiveConfig):  # stops may have sold since the last bar
                ledger.set_meta(SHIFT_META, repr(pending_shift(ledger) + change))
            else:
                guard.shift(change, equity)
                ledger.set_meta(GUARD_META, guard.state.model_dump_json())
                note_transfer(ledger, equity + change)
            ledger.add_event(now, "warning", f"{note}; booked as a transfer")
        ledger.set_meta(BUDGET_META, repr(config.initial_cash))


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


def base_cash(config: SessionConfig, ledger: Ledger) -> float:
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


def symbols_of(config: SessionConfig) -> list[str]:
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
        frames[key] = bars_since(store, key, count, until)
        if frames[key].height < count // WARMUP_MARGIN:
            hint = store.download_hint([key])
            raise ValueError(f"not enough stored bars for {key[0]} {key[1]}; run {hint}")
    histories = {
        key: BarHistory(key[1], max_bars=max(MAX_BARS, count)) for key, count in lookback.items()
    }
    session = TradingSession(config, slots, histories, portfolio)
    replay_history(session, frames, checkpoint)
    return session


def summary_text(
    session: TradingSession,
    ledger: Ledger,
    now: datetime,
    label: str,
    title: str = "daily summary",
    notes: Sequence[str] = (),
    store: BarStore | None = None,
) -> str:
    """Equity, its change, positions, and fills; coins moved in or out of the book are
    valued at their time from the store, as `tbot status` does, else at today's marks."""
    equity = session.portfolio.equity(session.marks)
    earlier = ledger.equity_before(now - timedelta(days=1))
    change = "no 24h reference"
    if earlier and earlier.equity > 0:

        def price(symbol: str, moment: datetime) -> float:
            if store is None:
                return session.marks.get(symbol, 0.0)
            return price_at(session.config, store, symbol, moment)

        moved = sum(  # deposits, withdrawals, budget changes: not a gain or a loss
            a.cash + a.quantity * price(a.symbol, a.time)
            for a in ledger.adjustments()
            if earlier.time < a.time <= now
        )
        change = f"{(equity - moved) / earlier.equity - 1:+.2%} over 24h"
    raw = ledger.get_meta(GUARD_META)
    guard = GuardState.model_validate_json(raw) if raw else GuardState()
    if guard.peak_equity > 0:
        change += f", {min(equity / guard.peak_equity - 1, 0.0):.1%} from peak"
    positions = ", ".join(
        f"{symbol} {qty:.6f} ({qty * session.marks.get(symbol, 0.0):,.0f})"
        for symbol, qty in sorted(session.portfolio.positions.items())
    )
    lines = [
        f"[{label}] {title} {now:%Y-%m-%d %H:%M} UTC",
        f"equity {equity:,.2f} ({change}), cash {session.portfolio.cash:,.2f}",
        f"positions: {positions or 'none'}",
        f"fills in 24h: {ledger.fill_count(now - timedelta(days=1))}",
    ]
    point = ledger.latest_equity()
    if point is not None:
        lines.append(f"last bar event {point.time:%Y-%m-%d %H:%M} UTC")
    if guard.halted:
        lines.append(f"HALTED: {guard.halt_reason}; `tbot resume` to trade again")
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


def command_answer(
    session: TradingSession,
    ledger: Ledger,
    label: str,
    clock: Callable[[], datetime],
    notes: Callable[[], list[str]],
    store: BarStore | None = None,
) -> Answer:
    def answer(command: str) -> str:
        if command in ("/status", "/start"):
            return summary_text(session, ledger, clock(), label, "status", notes(), store)
        if command == "/fills":
            return fills_text(ledger, label)
        return HELP

    return answer


async def summary_loop(
    session: TradingSession,
    ledger: Ledger,
    notifier: Notifier,
    hour: int,
    label: str,
    notes: Callable[[], list[str]] = list,
    store: BarStore | None = None,
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
            text = summary_text(session, ledger, utc_now(), label, notes=notes(), store=store)
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
                _lock(handle, lock=True)
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
            _lock(handle, lock=False)
    finally:
        handle.close()


def _lock(handle: IO[str], *, lock: bool) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK if lock else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), (fcntl.LOCK_EX | fcntl.LOCK_NB) if lock else fcntl.LOCK_UN)


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


Setup = Callable[[Context], Awaitable[tuple[Executor, list[Coroutine[Any, Any, None]]]]]


async def run_session(
    config: SessionConfig,
    settings: Settings,
    data_dir: Path,
    *,
    label: str,
    setup: Setup,
    stop: asyncio.Event | None = None,
) -> int:
    """Run until stopped. Returns a process exit code."""
    new_ledger = not config.ledger.exists()
    with instance_lock(config.ledger, wait=3.0):
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
            ledger.mark_written()
            return await _run_session(
                config,
                settings,
                data_dir,
                ledger,
                label=label,
                setup=setup,
                stop=stop,
                new_ledger=new_ledger,
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

            guard = load_guard(config, ledger)
            check_budget(config, ledger, guard, now)  # before the book: it may adjust it
            portfolio = restore_portfolio(config, ledger)
            checkpoint = load_checkpoint(config, ledger)
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
            )
            executor, extra = await setup(context)
        except Exception as exc:  # tell the operator; a supervisor would restart silently
            log.error("start_failed", error=repr(exc))
            ledger.add_event(utc_now(), "error", f"failed to start: {exc!r}")
            hint = f"; {key_hint(exc)}" if key_hint(exc) else ""
            if permanent(exc):  # the same at every try: say so, and let the supervisor stop
                await notifier.send(
                    f"[{label}] cannot start: {exc}{hint}; fix the config, then start"
                )
                return EXIT_REFUSED
            await notifier.send(f"[{label}] failed to start: {start_problem(exc)}{hint}")
            return 1
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
        )
        trader.quiet = lambda: quiet_streams(feed, getattr(executor, "spot", None))
        point = session.snapshot(now)
        where = config.ledger.resolve()
        ledger.add_event(now, "info", f"started ({label}), tbot {__version__}, ledger {where}")
        halted = f", HALTED: {guard.state.halt_reason}" if guard.state.halted else ""
        fresh = " (new)" if new_ledger else ""
        await notifier.send(
            f"[{label}] started (tbot {__version__}): {len(keys)} streams, "
            f"equity {point.equity:,.2f}, "
            f"positions {len(portfolio.positions)}, fills so far {len(portfolio.fills)}, "
            f"ledger {where}{fresh}{halted}"
        )

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
            return found

        hour = config.summary_hour_utc
        summary = summary_loop(session, ledger, notifier, hour, label, notes, store)
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
        ]
        if config.backup_days:
            backups = backup_loop(
                ledger, config.ledger, config.backup_days, lock, notifier, label, clock.now
            )
            tasks.append(asyncio.create_task(backups, name="backup"))
        if config.telegram_commands and isinstance(direct, Telegram):
            answer = command_answer(session, ledger, label, clock.now, notes, store)
            commands = command_loop(direct, answer, utc_now())
            tasks.append(asyncio.create_task(commands, name="commands"))
        elif config.telegram_commands and (settings.telegram_token or settings.telegram_chat_id):
            log.warning("telegram_commands_off", hint="set both TBOT_TELEGRAM_TOKEN and _CHAT_ID")
        if settings.heartbeat_url and not valid_url(settings.heartbeat_url):
            log.warning("heartbeat_url_invalid", hint=HEARTBEAT_URL_HINT)
        elif settings.heartbeat_url:
            spot = getattr(executor, "spot", None)

            def problem() -> str:
                return heartbeat_problem(feed.stale(), health.blocked, spot)

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
        ledger.add_event(utc_now(), "info", "stopped")
        equity = session.portfolio.equity(session.marks)
        await notifier.send(f"[{label}] stopped, equity {equity:,.2f}")
    return code


def quiet_streams(feed: LiveFeed, spot: BinanceSpot | None) -> set[StreamKey]:
    """Streams a close may never bring: stale ones, and pairs Binance does not trade."""
    found = set(feed.stale())
    if spot is not None:
        found |= {key for key in feed.keys if not spot.trading(key[0])}
    return found


def heartbeat_problem(
    stale: Sequence[StreamKey], blocked: str, spot: BinanceSpot | None = None
) -> str:
    """Why the heartbeat should not ping, or "": a stale stream or paused orders. A pair
    Binance pauses may send no bars while the rest trades on, so it does not count."""
    named = ", ".join(
        f"{symbol} {timeframe}"
        for symbol, timeframe in stale
        if spot is None or spot.trading(symbol)
    )
    return f"stale streams: {named}" if named else blocked


def valid_url(url: str) -> bool:
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        parsed = None
    return parsed is not None and parsed.scheme in ("http", "https") and bool(parsed.host)


async def run_paper(
    config: PaperConfig, settings: Settings, data_dir: Path, *, stop: asyncio.Event | None = None
) -> int:
    async def setup(ctx: Context) -> tuple[Executor, list[Coroutine[Any, Any, None]]]:
        executor = PaperExecutor(
            SimulatedBroker(config.costs),
            BinanceBookTicker(ctx.aclient),
            ctx.ledger,
            ctx.notifier,
            ctx.clock.now,
        )
        return executor, []

    return await run_session(config, settings, data_dir, label="paper", setup=setup, stop=stop)


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
        label=config.mode,
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
    """Reconcile every interval; after three failures in a row new orders wait."""
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
                await notifier.send(f"[{label}] {reason}; new orders wait until it works again")
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
    label = config.mode

    async def setup(ctx: Context) -> tuple[Executor, list[Coroutine[Any, Any, None]]]:
        async def resync() -> None:
            await asyncio.to_thread(ctx.clock.sync)

        spot = make_spot(config, settings, ctx.aclient, ctx.clock.now, resync)
        symbols = symbols_of(config)
        await spot.load_rules(symbols)
        if config.mode == "live":
            await check_key_rights(spot)
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

        async def reconcile_once() -> None:
            await reconcile_round(
                executor,
                portfolio,
                ctx.session.marks,
                ctx.ledger,
                ctx.notifier,
                ctx.guard,
                config,
                ctx.clock.now,
            )

        async with ctx.lock:
            await reconcile_once()  # a key Binance rejects for good ends the start (permanent)
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

    return await run_session(config, settings, data_dir, label=label, setup=setup, stop=stop)


async def check_key_rights(spot: BinanceSpot) -> None:
    """Real money only with a key that cannot withdraw: a stolen one could empty the
    account. Rights Binance does not show leave the start to go on; doctor says so."""
    try:
        rights = await spot.api_restrictions()
    except (httpx.HTTPError, BinanceError) as exc:
        log.warning("key_rights_unknown", error=repr(exc))
        return
    if rights.get("enableWithdrawals", True):
        raise StartRefused(
            "the API key can withdraw: turn off withdrawals for it in Binance API Management"
        )
    if not rights.get("enableSpotAndMarginTrading", False):
        raise StartRefused("the API key cannot trade spot: turn on spot trading for it")


def resume(config: SessionConfig) -> str:
    """Clear the kill switch. Returns a message for the operator."""
    ledger = Ledger(config.ledger)
    try:
        guard = load_guard(config, ledger)
        if not guard.state.halted:
            return "not halted"
        reason = guard.state.halt_reason
        guard.resume()
        ledger.set_meta(GUARD_META, guard.state.model_dump_json())
        ledger.add_event(utc_now(), "info", f"resumed after halt: {reason}")
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
    """Warn when the ledger was started under a different trading config."""
    current = config_hash(config.model_dump_json(exclude=OPERATIONAL))
    stored = ledger.get_meta("config_hash")
    if stored is None:
        ledger.set_meta("config_hash", current)
        ledger.set_meta("created_at", utc_now().isoformat())
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


def _send_later(notifier: Notifier, message: str) -> None:
    """Send from code that cannot wait, such as a feed callback."""
    task = asyncio.get_running_loop().create_task(notifier.send(message))
    _background.add(task)  # a bare task can be collected before it runs
    task.add_done_callback(_background.discard)


def status_text(config: SessionConfig, store: BarStore) -> str:
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
            lines.append(f"HALTED: {guard.state.halt_reason} (run `tbot resume`)")
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
