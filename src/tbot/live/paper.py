"""Paper trading: live bars in, simulated fills at the current book price."""

import asyncio
import signal
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

import httpx
import polars as pl
import structlog

from tbot.backtest.runner import WARMUP_MARGIN
from tbot.core.models import Fill
from tbot.data.downloader import sync
from tbot.data.store import BarStore
from tbot.execution.sim_broker import SimulatedBroker
from tbot.live.clock import ServerClock
from tbot.live.config import PaperConfig, Settings
from tbot.live.feed import Batch, LiveFeed, bars_since, close_time, utc_now
from tbot.live.history import BarHistory
from tbot.live.ledger import Ledger, config_hash
from tbot.live.session import StreamKey, TradingSession, replay_history
from tbot.monitoring.heartbeat import heartbeat_loop
from tbot.monitoring.telegram import LogNotifier, Notifier, Telegram
from tbot.portfolio.allocation import StrategySlot, build_slots
from tbot.portfolio.portfolio import Portfolio

log = structlog.get_logger(__name__)
BOOK_TICKER_URL = "https://data-api.binance.vision/api/v3/ticker/bookTicker"
CLOCK_RESYNC_SECONDS = 600


class PriceSource(Protocol):
    async def price(self, symbol: str, side: int) -> float:
        """Executable price now: ask for a buy (side > 0), bid for a sell."""


class BinanceBookTicker:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def price(self, symbol: str, side: int) -> float:
        response = await self.client.get(BOOK_TICKER_URL, params={"symbol": symbol}, timeout=10.0)
        response.raise_for_status()
        data = response.json()
        return float(data["askPrice"] if side > 0 else data["bidPrice"])


class PaperTrader:
    """Turns each batch of closed bars into decisions, simulated fills, and ledger rows."""

    def __init__(
        self,
        session: TradingSession,
        ledger: Ledger,
        broker: SimulatedBroker,
        prices: PriceSource,
        notifier: Notifier,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.session = session
        self.ledger = ledger
        self.broker = broker
        self.prices = prices
        self.notifier = notifier
        self.clock = clock

    async def handle(self, batch: Batch) -> list[Fill]:
        now = max(close_time(key, bar) for key, bar in batch.items())
        orders = self.session.ingest(batch, now)
        for slot in self.session.slots:
            if any((s, slot.timeframe) in batch for s in slot.strategy.symbols):
                self.ledger.add_signals(now, slot.strategy.name, slot.targets)
        fills = await self.execute(orders, now)
        self.ledger.add_equity(self.session.snapshot(now))
        log.info(
            "bar_event",
            time=now.isoformat(),
            streams=[f"{s}:{tf}" for s, tf in batch],
            orders=len(orders),
            fills=len(fills),
            equity=round(self.session.portfolio.equity(self.session.marks), 2),
        )
        return fills

    async def execute(self, orders: dict[str, float], now: datetime) -> list[Fill]:
        fills = []
        portfolio = self.session.portfolio
        # Sells first so their proceeds can fund buys.
        for symbol, quantity in sorted(orders.items(), key=lambda item: item[1]):
            try:
                reference = await self.prices.price(symbol, 1 if quantity > 0 else -1)
            except Exception as exc:  # a failed quote skips this order only
                log.error("price_failed", symbol=symbol, error=repr(exc))
                self.ledger.add_order(now, symbol, quantity, "failed", repr(exc))
                await self.notifier.send(f"[paper] price lookup failed for {symbol}: {exc!r}")
                continue
            fill = self.broker.fill(
                self.clock(),
                symbol,
                quantity,
                reference,
                portfolio.cash,
                portfolio.position(symbol),
            )
            if fill is None:
                self.ledger.add_order(now, symbol, quantity, "skipped", "no cash or position")
                continue
            portfolio.apply(fill)
            self.ledger.add_fill(fill, reference)
            self.ledger.add_order(now, symbol, quantity, "filled")
            side = "BUY" if fill.quantity > 0 else "SELL"
            await self.notifier.send(
                f"[paper] {side} {abs(fill.quantity):.6f} {symbol} @ {fill.price:,.2f} "
                f"fee {fill.fee:.2f} | equity {portfolio.equity(self.session.marks):,.2f}"
            )
            fills.append(fill)
        return fills


def stream_keys(config: PaperConfig) -> list[StreamKey]:
    keys = {(symbol, c.timeframe) for c in config.strategies for symbol in c.symbols}
    return sorted(keys, key=lambda key: (key[1].millis, key[0]))


def lookback_bars(slots: Sequence[StrategySlot]) -> dict[StreamKey, int]:
    lookback: dict[StreamKey, int] = {}
    for slot in slots:
        for symbol in slot.strategy.symbols:
            key = (symbol, slot.timeframe)
            lookback[key] = max(lookback.get(key, 0), slot.strategy.warmup * WARMUP_MARGIN)
    return lookback


def restore_portfolio(config: PaperConfig, ledger: Ledger) -> Portfolio:
    """The ledger's fills are the source of truth; replaying them rebuilds the portfolio."""
    portfolio = Portfolio(config.initial_cash)
    for fill in ledger.fills():
        portfolio.apply(fill)
    return portfolio


def build_session(
    config: PaperConfig, store: BarStore, portfolio: Portfolio, now: datetime
) -> TradingSession:
    slots = build_slots(config.strategies)
    lookback = lookback_bars(slots)
    frames: dict[StreamKey, pl.DataFrame] = {}
    for key, count in lookback.items():
        frames[key] = bars_since(store, key, count, now)
        if frames[key].height < count // WARMUP_MARGIN:
            raise ValueError(f"not enough stored bars for {key[0]} {key[1]}; run `tbot download`")
    histories = {key: BarHistory(key[1]) for key in lookback}
    session = TradingSession(config, slots, histories, portfolio)
    replay_history(session, frames)
    return session


def summary_text(session: TradingSession, ledger: Ledger, now: datetime) -> str:
    equity = session.portfolio.equity(session.marks)
    earlier = ledger.equity_before(now - timedelta(days=1))
    change = f"{equity / earlier.equity - 1:+.2%} over 24h" if earlier else "no 24h reference"
    positions = ", ".join(
        f"{symbol} {qty:.6f} ({qty * session.marks[symbol]:,.0f})"
        for symbol, qty in sorted(session.portfolio.positions.items())
    )
    return (
        f"[paper] daily summary {now:%Y-%m-%d %H:%M} UTC\n"
        f"equity {equity:,.2f} ({change}), cash {session.portfolio.cash:,.2f}\n"
        f"positions: {positions or 'none'}"
    )


async def summary_loop(
    session: TradingSession, ledger: Ledger, notifier: Notifier, hour: int
) -> None:
    while True:
        now = utc_now()
        target = now.replace(hour=hour, minute=5, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        await notifier.send(summary_text(session, ledger, utc_now()))


async def clock_loop(clock: ServerClock, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(clock.sync)
        except (httpx.HTTPError, LookupError, ValueError) as exc:
            log.warning("clock_sync_failed", error=repr(exc))


async def consume(feed: LiveFeed, trader: PaperTrader) -> None:
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


async def run_paper(
    config: PaperConfig,
    settings: Settings,
    data_dir: Path,
    *,
    stop: asyncio.Event | None = None,
) -> int:
    """Run until stopped. Returns a process exit code."""
    stop = stop or asyncio.Event()
    install_stop_handlers(stop)
    store = BarStore(data_dir)
    keys = stream_keys(config)
    ledger = Ledger(config.ledger)
    async with httpx.AsyncClient() as aclient:
        notifier = make_notifier(settings, aclient)
        with httpx.Client(timeout=30.0) as client:
            clock = ServerClock(client)
            await asyncio.to_thread(clock.sync)
            now = clock.now()
            log.info("syncing_store", streams=[f"{s}:{tf}" for s, tf in keys])
            for symbol, timeframe in keys:
                start = now - timeframe.delta * 400
                await asyncio.to_thread(sync, store, client, symbol, timeframe, start, now)

            portfolio = restore_portfolio(config, ledger)
            session = build_session(config, store, portfolio, now)
            _check_config(config, ledger)

            feed = LiveFeed(
                keys,
                store,
                client,
                clock=clock.now,
                stale_after=config.stale_after_seconds,
                batch_wait=config.batch_wait_seconds,
                on_stale=lambda key, last: _report_stale(ledger, notifier, key, last),
            )
            trader = PaperTrader(
                session,
                ledger,
                SimulatedBroker(config.costs),
                BinanceBookTicker(aclient),
                notifier,
                clock=clock.now,
            )
            point = session.snapshot(now)
            ledger.add_event(now, "info", "started")
            await notifier.send(
                f"[paper] started: {len(keys)} streams, equity {point.equity:,.2f}, "
                f"positions {len(portfolio.positions)}, fills so far {len(portfolio.fills)}"
            )

            tasks = [
                asyncio.create_task(clock_loop(clock, CLOCK_RESYNC_SECONDS), name="clock"),
                asyncio.create_task(feed.run_websocket(), name="websocket"),
                asyncio.create_task(feed.run_watchdog(), name="watchdog"),
                asyncio.create_task(consume(feed, trader), name="consume"),
                asyncio.create_task(
                    summary_loop(session, ledger, notifier, config.summary_hour_utc), name="summary"
                ),
            ]
            if settings.heartbeat_url:
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
                    await notifier.send(f"[paper] CRASHED in {task.get_name()}: {error}")
                    code = 1
            for task in [*tasks, stopper]:
                task.cancel()
            await asyncio.gather(*tasks, stopper, return_exceptions=True)
            ledger.add_event(utc_now(), "info", "stopped")
            await notifier.send(
                f"[paper] stopped, equity {session.portfolio.equity(session.marks):,.2f}"
            )
    ledger.close()
    return code


def _check_config(config: PaperConfig, ledger: Ledger) -> None:
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


def _report_stale(ledger: Ledger, notifier: Notifier, key: StreamKey, last: datetime) -> None:
    symbol, timeframe = key
    message = (
        f"[paper] stale stream {symbol} {timeframe}: last closed bar {last:%Y-%m-%d %H:%M} UTC"
    )
    log.warning("stale_stream", symbol=symbol, timeframe=str(timeframe), last=last.isoformat())
    ledger.add_event(utc_now(), "warning", message)
    asyncio.get_running_loop().create_task(notifier.send(message))


def status_text(config: PaperConfig, store: BarStore) -> str:
    ledger = Ledger(config.ledger)
    try:
        portfolio = restore_portfolio(config, ledger)
        marks = {key[0]: store.last_open_time(*key) for key in stream_keys(config)}
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
            f"fills {len(portfolio.fills)}, round trips {len(portfolio.trades)}",
        ]
        for symbol, qty in sorted(portfolio.positions.items()):
            lines.append(f"position {symbol} {qty:.6f}")
        for symbol, last in marks.items():
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
