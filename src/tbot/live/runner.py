"""Paper, testnet, and live sessions: one event loop, different executors."""

import asyncio
import signal
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import structlog

from tbot.backtest.runner import WARMUP_MARGIN, history_bars
from tbot.core.models import Fill
from tbot.data.downloader import sync
from tbot.data.store import BarStore
from tbot.exchange.binance import PRODUCTION_URL, TESTNET_URL, BinanceError, BinanceSpot
from tbot.execution.sim_broker import SimulatedBroker
from tbot.live.clock import ServerClock
from tbot.live.config import LiveConfig, PaperConfig, SessionConfig, Settings
from tbot.live.executor import BinanceBookTicker, Executor, LiveExecutor, PaperExecutor
from tbot.live.feed import Batch, LiveFeed, bars_since, close_time, utc_now
from tbot.live.guard import GuardState, Mode, RiskGuard
from tbot.live.history import BarHistory
from tbot.live.ledger import Adjustment, Ledger, config_hash
from tbot.live.reconcile import reconcile
from tbot.live.session import StreamKey, TradingSession, replay_history
from tbot.monitoring.heartbeat import heartbeat_loop
from tbot.monitoring.telegram import LogNotifier, Notifier, Telegram
from tbot.portfolio.allocation import StrategySlot, build_slots
from tbot.portfolio.portfolio import Portfolio
from tbot.risk.limits import RiskLimits

log = structlog.get_logger(__name__)
CLOCK_RESYNC_SECONDS = 600
SYNC_LOOKBACK_BARS = 400
GUARD_META = "guard"
_background: set[asyncio.Task[bool]] = set()


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
    ) -> None:
        self.session = session
        self.ledger = ledger
        self.executor = executor
        self.notifier = notifier
        self.guard = guard
        self.clock = clock
        self.label = label
        self.lock = lock or asyncio.Lock()  # one writer to the book at a time

    async def handle(self, batch: Batch) -> list[Fill]:
        async with self.lock:
            return await self._handle(batch)

    async def _handle(self, batch: Batch) -> list[Fill]:
        session = self.session
        now = max(close_time(key, bar) for key, bar in batch.items())
        orders = session.ingest(batch, now)
        for slot in session.slots:
            if any((s, slot.timeframe) in batch for s in slot.strategy.symbols):
                self.ledger.add_signals(now, slot.strategy.name, slot.targets)

        self.reload_guard()
        was_halted = self.guard.state.halted
        decision = self.guard.check(self.clock(), session.portfolio.equity(session.marks), now)
        self.save_guard()
        fills: list[Fill] = []
        if decision.mode == Mode.HALT:
            fills = await self._halt(decision.reason, now, announce=not was_halted)
        elif decision.mode == Mode.BLOCK:
            self.ledger.add_event(now, "warning", f"blocked: {decision.reason}")
            await self.notifier.send(f"[{self.label}] no trading this bar: {decision.reason}")
        else:
            allowed = self.guard.filter_orders(orders, session.portfolio.positions, decision.mode)
            if decision.mode == Mode.REDUCE_ONLY and allowed != dict(orders):
                self.ledger.add_event(now, "warning", f"reduce only: {decision.reason}")
                await self.notifier.send(f"[{self.label}] entries blocked: {decision.reason}")
            fills = await self.executor.execute(allowed, now, session.portfolio, session.marks)
            await self.executor.after_event(session.portfolio, session.marks)
        self.ledger.add_equity(session.snapshot(now))
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
            self.ledger.add_event(now, "error", f"kill switch: {reason}")
            await self.notifier.send(
                f"[{self.label}] KILL SWITCH: {reason}. Positions flattened ({len(fills)} "
                f"fills). Trading stays halted until `tbot resume`."
            )
        return fills

    def save_guard(self) -> None:
        self.ledger.set_meta(GUARD_META, self.guard.state.model_dump_json())

    def reload_guard(self) -> None:
        """Adopt a `tbot resume` issued while this process runs."""
        if not self.guard.state.halted:
            return
        raw = self.ledger.get_meta(GUARD_META)
        if raw:
            stored = GuardState.model_validate_json(raw)
            if not stored.halted:
                self.guard.state = stored


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
    portfolio = Portfolio(config.initial_cash)
    events: list[tuple[datetime, int, Fill | Adjustment]] = []
    events.extend((f.time, 0, f) for f in ledger.fills())
    events.extend((a.time, 1, a) for a in ledger.adjustments())
    for _, _, item in sorted(events, key=lambda e: (e[0], e[1])):
        if isinstance(item, Fill):
            portfolio.apply(item)
        else:
            portfolio.adjust(item.symbol, item.quantity, item.cash)
    return portfolio


def build_session(
    config: SessionConfig, store: BarStore, portfolio: Portfolio, now: datetime
) -> TradingSession:
    slots = build_slots(config.strategies)
    lookback = lookback_bars(slots, config.risk)
    frames: dict[StreamKey, pl.DataFrame] = {}
    for key, count in lookback.items():
        frames[key] = bars_since(store, key, count, now)
        if frames[key].height < count // WARMUP_MARGIN:
            raise ValueError(f"not enough stored bars for {key[0]} {key[1]}; run `tbot download`")
    histories = {key: BarHistory(key[1]) for key in lookback}
    session = TradingSession(config, slots, histories, portfolio)
    replay_history(session, frames)
    return session


def summary_text(session: TradingSession, ledger: Ledger, now: datetime, label: str) -> str:
    equity = session.portfolio.equity(session.marks)
    earlier = ledger.equity_before(now - timedelta(days=1))
    change = f"{equity / earlier.equity - 1:+.2%} over 24h" if earlier else "no 24h reference"
    positions = ", ".join(
        f"{symbol} {qty:.6f} ({qty * session.marks[symbol]:,.0f})"
        for symbol, qty in sorted(session.portfolio.positions.items())
    )
    return (
        f"[{label}] daily summary {now:%Y-%m-%d %H:%M} UTC\n"
        f"equity {equity:,.2f} ({change}), cash {session.portfolio.cash:,.2f}\n"
        f"positions: {positions or 'none'}"
    )


async def summary_loop(
    session: TradingSession, ledger: Ledger, notifier: Notifier, hour: int, label: str
) -> None:
    while True:
        now = utc_now()
        target = now.replace(hour=hour, minute=5, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        await notifier.send(summary_text(session, ledger, utc_now(), label))


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
    log.warning("telegram_not_configured", hint="set TBOT_TELEGRAM_TOKEN and TBOT_TELEGRAM_CHAT_ID")
    return LogNotifier()


def install_stop_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows event loops
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


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
    ledger = Ledger(config.ledger)
    try:
        return await _run_session(
            config, settings, data_dir, ledger, label=label, setup=setup, stop=stop
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
) -> int:
    stop = stop or asyncio.Event()
    install_stop_handlers(stop)
    store = BarStore(data_dir)
    keys = stream_keys(config)
    lock = asyncio.Lock()
    async with httpx.AsyncClient() as aclient:
        notifier = make_notifier(settings, aclient)
        with httpx.Client(timeout=30.0) as client:
            clock = ServerClock(client)
            await asyncio.to_thread(clock.sync)
            now = clock.now()
            log.info("syncing_store", streams=[f"{s}:{tf}" for s, tf in keys])
            for symbol, timeframe in keys:
                start = now - timeframe.delta * SYNC_LOOKBACK_BARS
                await asyncio.to_thread(sync, store, client, symbol, timeframe, start, now)

            portfolio = restore_portfolio(config, ledger)
            session = build_session(config, store, portfolio, now)
            _check_config(config, ledger)
            context = Context(
                config, settings, session, ledger, notifier, clock, aclient, keys, label, lock
            )
            executor, extra = await setup(context)
            guard = load_guard(config, ledger)
            trader = SessionTrader(
                session, ledger, executor, notifier, guard, clock.now, label, lock=lock
            )

            feed = LiveFeed(
                keys,
                store,
                client,
                clock=clock.now,
                stale_after=config.stale_after_seconds,
                batch_wait=config.batch_wait_seconds,
                on_stale=lambda key, last: _report_stale(ledger, notifier, label, key, last),
            )
            point = session.snapshot(now)
            ledger.add_event(now, "info", f"started ({label})")
            halted = f", HALTED: {guard.state.halt_reason}" if guard.state.halted else ""
            await notifier.send(
                f"[{label}] started: {len(keys)} streams, equity {point.equity:,.2f}, "
                f"positions {len(portfolio.positions)}, fills so far {len(portfolio.fills)}{halted}"
            )

            tasks = [
                asyncio.create_task(clock_loop(clock, CLOCK_RESYNC_SECONDS), name="clock"),
                asyncio.create_task(feed.run_websocket(), name="websocket"),
                asyncio.create_task(feed.run_watchdog(), name="watchdog"),
                asyncio.create_task(consume(feed, trader), name="consume"),
                asyncio.create_task(
                    summary_loop(session, ledger, notifier, config.summary_hour_utc, label),
                    name="summary",
                ),
                *(asyncio.create_task(coro, name=f"extra{i}") for i, coro in enumerate(extra)),
            ]
            if settings.heartbeat_url and _valid_url(settings.heartbeat_url):
                tasks.append(
                    asyncio.create_task(
                        heartbeat_loop(settings.heartbeat_url, config.heartbeat_seconds, aclient),
                        name="heartbeat",
                    )
                )
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
            for task in [*tasks, stopper]:
                task.cancel()
            await asyncio.gather(*tasks, stopper, return_exceptions=True)
            ledger.add_event(utc_now(), "info", "stopped")
            equity = session.portfolio.equity(session.marks)
            await notifier.send(f"[{label}] stopped, equity {equity:,.2f}")
    return code


def _valid_url(url: str) -> bool:
    try:
        httpx.URL(url)
    except httpx.InvalidURL:
        log.warning("heartbeat_url_invalid", hint="check TBOT_HEARTBEAT_URL")
        return False
    return True


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
    config: LiveConfig, settings: Settings, client: httpx.AsyncClient, clock: Callable[[], datetime]
) -> BinanceSpot:
    if not settings.binance_api_key or not settings.binance_api_secret:
        raise ValueError("set TBOT_BINANCE_API_KEY and TBOT_BINANCE_API_SECRET")
    return BinanceSpot(
        client,
        settings.binance_api_key,
        settings.binance_api_secret,
        base_url=TESTNET_URL if config.mode == "testnet" else PRODUCTION_URL,
        clock=clock,
        recv_window=config.recv_window,
    )


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
    label = config.mode

    async def setup(ctx: Context) -> tuple[Executor, list[Coroutine[Any, Any, None]]]:
        spot = make_spot(config, settings, ctx.aclient, ctx.clock.now)
        symbols = symbols_of(config)
        await spot.load_rules(symbols)
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
            await reconcile(
                spot,
                portfolio,
                symbols,
                ctx.ledger,
                ctx.notifier,
                ctx.clock.now(),
                tolerance=config.reconcile_tolerance,
                label=label,
            )

        await reconcile_once()
        await executor.after_event(portfolio, ctx.session.marks)  # stops for held positions

        async def reconcile_loop() -> None:
            while True:
                await asyncio.sleep(config.reconcile_seconds)
                try:
                    async with ctx.lock:  # never while an order is in flight
                        await reconcile_once()
                except (BinanceError, httpx.HTTPError) as exc:
                    log.warning("reconcile_failed", error=repr(exc))

        return executor, [reconcile_loop()]

    return await run_session(config, settings, data_dir, label=label, setup=setup, stop=stop)


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
                    f"open {symbol} {order.type} {order.side} {order.executed_qty:.6f} "
                    f"stop {order.stop_price} id {order.client_order_id}"
                )
        return "\n".join(lines)


def _check_config(config: SessionConfig, ledger: Ledger) -> None:
    """Warn when the ledger was started under a different trading config."""
    current = config_hash(config.model_dump_json(exclude={"ledger"}))
    stored = ledger.get_meta("config_hash")
    if stored is None:
        ledger.set_meta("config_hash", current)
        ledger.set_meta("created_at", utc_now().isoformat())
    elif stored != current:
        log.warning("config_changed", stored=stored, current=current)
        ledger.add_event(utc_now(), "warning", f"config changed: {stored} -> {current}")
        ledger.set_meta("config_hash", current)


def _report_stale(
    ledger: Ledger, notifier: Notifier, label: str, key: StreamKey, last: datetime
) -> None:
    symbol, timeframe = key
    message = (
        f"[{label}] stale stream {symbol} {timeframe}: last closed bar {last:%Y-%m-%d %H:%M} UTC"
    )
    log.warning("stale_stream", symbol=symbol, timeframe=str(timeframe), last=last.isoformat())
    ledger.add_event(utc_now(), "warning", message)
    task = asyncio.get_running_loop().create_task(notifier.send(message))
    _background.add(task)  # a bare task can be collected before it runs
    task.add_done_callback(_background.discard)


def status_text(config: SessionConfig, store: BarStore) -> str:
    ledger = Ledger(config.ledger)
    try:
        portfolio = restore_portfolio(config, ledger)
        guard = load_guard(config, ledger)
        point = ledger.latest_equity()
        lines = [
            f"ledger {config.ledger}",
            f"created {ledger.get_meta('created_at') or '-'}",
            (
                f"equity {point.equity:,.2f} at {point.time:%Y-%m-%d %H:%M} UTC, "
                f"cash {point.cash:,.2f}, exposure {point.exposure:.1%}"
                if point
                else "no equity snapshots yet"
            ),
            f"fills {len(portfolio.fills)}, round trips {len(portfolio.trades)}, "
            f"adjustments {len(ledger.adjustments())}",
        ]
        if guard.state.halted:
            lines.append(f"HALTED: {guard.state.halt_reason} (run `tbot resume`)")
        for symbol, qty in sorted(portfolio.positions.items()):
            lines.append(f"position {symbol} {qty:.6f}")
        for symbol, timeframe in stream_keys(config):
            last = store.last_open_time(symbol, timeframe)
            lines.append(
                f"last stored bar {symbol}: {last:%Y-%m-%d %H:%M}" if last else f"no bars {symbol}"
            )
        for fill in ledger.recent_fills(5):
            side = "BUY" if fill.quantity > 0 else "SELL"
            lines.append(
                f"fill {fill.time:%Y-%m-%d %H:%M} {side} {abs(fill.quantity):.6f} "
                f"{fill.symbol} @ {fill.price:,.2f}"
            )
        for event in ledger.recent_events(5):
            lines.append(f"event {event.time:%Y-%m-%d %H:%M} {event.level}: {event.message}")
        return "\n".join(lines)
    finally:
        ledger.close()
