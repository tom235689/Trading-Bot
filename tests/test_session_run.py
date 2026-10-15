"""A whole session, start to stop, with the network replaced: the code tests rarely reach."""

import asyncio
import json
import signal
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import polars as pl
import pytest

from binance_spot_fake import DEFAULT_RULES, FakeSpot
from factories import price_bars
from tbot.core.config import StrategyConfig
from tbot.core.models import Fill
from tbot.core.timeframe import Timeframe
from tbot.data.schema import empty_bars
from tbot.data.store import BarStore
from tbot.exchange.binance import INVALID_SYMBOL, TESTNET_URL, BinanceError, BinanceSpot
from tbot.execution.sim_broker import SimulatedBroker
from tbot.live import runner
from tbot.live.clock import ServerClock
from tbot.live.config import LiveConfig, PaperConfig, Settings
from tbot.live.executor import Executor, PaperExecutor
from tbot.live.feed import LiveFeed
from tbot.live.ledger import EquityPoint, Ledger
from tbot.live.runner import (
    GUARD_META,
    HALT_BY_HAND,
    MODE_META,
    SUPERVISED_META,
    Context,
    Health,
    LedgerModeError,
    StartRefused,
    halt_path,
    reconcile_loop,
    registered_sessions,
    run_paper,
    run_session,
    stop_path,
)
from tbot.monitoring.dashboard import growth
from tbot.risk.guard import GuardConfig, GuardState

H4 = Timeframe.H4
KEY = ("BTCUSDT", H4)


class Collect:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


class Prices:
    async def price(self, symbol: str, side: int) -> float:
        return 130.0


@asynccontextmanager
async def silent(url: str) -> AsyncIterator[AsyncIterator[str | bytes]]:
    async def stream() -> AsyncIterator[str | bytes]:
        await asyncio.Event().wait()  # connected; nothing arrives
        yield ""

    yield stream()


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Collect, list[LiveFeed]]]:
    """Everything the session would fetch over the network, replaced."""
    notifier = Collect()
    feeds: list[LiveFeed] = []

    class Feed(LiveFeed):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, connector=silent, **kwargs)
            feeds.append(self)

    monkeypatch.setattr(ServerClock, "sync", lambda self: 0.0)
    monkeypatch.setattr(runner, "sync", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "make_notifier", lambda settings, client: notifier)
    monkeypatch.setattr(runner, "BinanceBookTicker", lambda client: Prices())
    monkeypatch.setattr(runner, "LiveFeed", Feed)
    monkeypatch.setattr("tbot.live.feed.fetch_klines", lambda *args: empty_bars())
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    yield notifier, feeds
    for sig, handler in handlers.items():  # the session installs its own
        signal.signal(sig, handler)


def setup_store(tmp_path: Path) -> tuple[PaperConfig, BarStore, pl.DataFrame]:
    """Flat history up to the last close, and a breakout bar the feed will hand over."""
    end = H4.floor(datetime.now(UTC))
    count = 40
    start = end - H4.delta * count
    closes = [100.0] * count
    store = BarStore(tmp_path / "data")
    store.write(*KEY, price_bars(start, H4, closes, closes))
    breakout = price_bars(end, H4, [100.0], [130.0])
    config = PaperConfig(
        initial_cash=1000.0,
        strategies=[
            StrategyConfig(
                name="donchian_trend",
                symbols=["BTCUSDT"],
                timeframe=H4,
                allocation=1.0,
                params={"entry": 5, "exit": 3},
            )
        ],
        ledger=tmp_path / "paper.sqlite",
        guard=GuardConfig(daily_loss_limit=0, max_drawdown=0, stale_seconds=0),
        backup_days=0,
    )
    return config, store, breakout


async def until(condition: Callable[[], bool], seconds: float = 10.0) -> None:
    async with asyncio.timeout(seconds):
        while not condition():
            await asyncio.sleep(0.02)


def test_a_paper_session_trades_a_bar_and_stops_cleanly(
    tmp_path: Path, offline: tuple[Collect, list[LiveFeed]]
) -> None:
    notifier, feeds = offline
    config, _, breakout = setup_store(tmp_path)

    async def scenario() -> int:
        stop = asyncio.Event()
        session = asyncio.create_task(
            run_paper(config, Settings(_env_file=None), tmp_path / "data", stop=stop)  # type: ignore[call-arg]
        )
        await until(lambda: bool(feeds) and any("started" in m for m in notifier.messages))
        feeds[0].queue.put_nowait((KEY, breakout))
        await until(lambda: any("BUY" in m for m in notifier.messages))
        stop.set()
        return await session

    assert asyncio.run(scenario()) == 0
    assert notifier.messages[0].startswith("[paper] started")
    assert notifier.messages[-1].startswith("[paper] stopped")  # sent before the exit
    with Ledger(config.ledger) as ledger:
        assert ledger.counts()["fills"] == 1
        assert ledger.latest_equity() is not None


def test_a_halted_book_sells_at_the_start_and_a_halt_request_reaches_it(
    tmp_path: Path, offline: tuple[Collect, list[LiveFeed]], monkeypatch: pytest.MonkeyPatch
) -> None:
    notifier, _ = offline
    config, _, _ = setup_store(tmp_path)
    monkeypatch.setenv("TBOT_SUPERVISED", "1")
    monkeypatch.setattr(runner, "STOP_POLL_SECONDS", 0.05)
    with Ledger(config.ledger) as ledger:  # `tbot halt` while it was stopped, holding coins
        ledger.add_fill(Fill(datetime(2024, 1, 1, tzinfo=UTC), "BTCUSDT", 2.0, 100.0, 0.2), 100)
        state = GuardState(peak_equity=1000.0, halted=True, halt_reason=HALT_BY_HAND)
        ledger.set_meta(GUARD_META, state.model_dump_json())

    async def scenario() -> int:
        stop = asyncio.Event()
        session = asyncio.create_task(
            run_paper(config, Settings(_env_file=None), tmp_path / "data", stop=stop)  # type: ignore[call-arg]
        )
        await until(lambda: any("SELL 2.000000 BTCUSDT" in m for m in notifier.messages))
        halt_path(config.ledger).write_text("halt\n", encoding="utf-8")  # halted already
        await until(lambda: not halt_path(config.ledger).exists())
        stop.set()
        return await session

    assert asyncio.run(scenario()) == 0
    assert notifier.messages[-1].endswith(", nothing held")
    assert sum("KILL SWITCH" in m for m in notifier.messages) == 1  # sold once, told once
    with Ledger(config.ledger) as ledger:
        assert ledger.get_meta(SUPERVISED_META) == "1"  # `tbot stop` says it comes back


def test_a_start_by_hand_drops_a_stop_request_left_for_a_supervisor(
    tmp_path: Path, offline: tuple[Collect, list[LiveFeed]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TBOT_SUPERVISED", raising=False)
    notifier, _ = offline
    config, _, _ = setup_store(tmp_path)
    stop_path(config.ledger).write_text("stop\n", encoding="utf-8")  # an earlier `tbot stop`

    async def scenario() -> int:
        stop = asyncio.Event()
        session = asyncio.create_task(
            run_paper(config, Settings(_env_file=None), tmp_path / "data", stop=stop)  # type: ignore[call-arg]
        )
        await until(lambda: any("started" in m for m in notifier.messages))
        stop.set()
        return await session

    assert asyncio.run(scenario()) == 0
    assert not stop_path(config.ledger).exists()  # taken away, not obeyed


def test_a_crashed_task_ends_the_session_with_an_alert(
    tmp_path: Path, offline: tuple[Collect, list[LiveFeed]]
) -> None:
    notifier, _ = offline
    config, _, _ = setup_store(tmp_path)

    async def crash() -> None:
        raise RuntimeError("boom")

    async def setup(ctx: Context) -> tuple[Executor, list[Coroutine[Any, Any, None]]]:
        executor = PaperExecutor(
            SimulatedBroker(config.costs), Prices(), ctx.ledger, ctx.notifier, ctx.clock.now
        )
        return executor, [crash()]

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    code = asyncio.run(run_session(config, settings, tmp_path / "data", label="paper", setup=setup))
    assert code == 1  # the supervisor starts it again
    assert any("CRASHED in extra0" in m for m in notifier.messages)
    assert notifier.messages[-1].startswith("[paper] stopped")


def test_a_failed_start_is_alerted(tmp_path: Path, offline: tuple[Collect, list[LiveFeed]]) -> None:
    notifier, _ = offline
    config, _, _ = setup_store(tmp_path)

    async def setup(ctx: Context) -> tuple[Executor, list[Coroutine[Any, Any, None]]]:
        raise ValueError("no keys")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    code = asyncio.run(run_session(config, settings, tmp_path / "data", label="paper", setup=setup))
    assert code == 1
    assert notifier.messages == ["[paper] failed to start: no keys"]


def test_failing_reconciliations_pause_new_orders_until_one_works(tmp_path: Path) -> None:
    results = [RuntimeError("down")] * 3 + [None]
    health = Health()
    notifier = Collect()
    seen: list[str] = []

    async def round_() -> None:
        outcome = results.pop(0) if results else None
        seen.append(health.blocked)
        if outcome is not None:
            raise outcome

    async def scenario() -> None:
        with Ledger(tmp_path / "live.sqlite") as ledger:
            loop = asyncio.create_task(
                reconcile_loop(round_, 0, asyncio.Lock(), health, ledger, notifier, "live")
            )
            await until(lambda: len(seen) >= 5)
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)

    asyncio.run(scenario())
    assert seen[:4] == ["", "", "", "reconciliation failing (down)"]  # paused after three
    assert health.blocked == ""  # the fourth worked
    assert notifier.messages == [
        "[live] reconciliation failing (down); new buys wait until it works again, "
        "sales still go out",
        "[live] reconciliation works again",
    ]


def test_a_network_error_at_start_is_told_in_one_line() -> None:
    request = httpx.Request("GET", "https://data-api.binance.vision/api/v3/time?secret=x")
    forbidden = httpx.HTTPStatusError(
        "403", request=request, response=httpx.Response(403, request=request)
    )
    assert runner.start_problem(forbidden) == (  # Binance's firewall: it passes
        "HTTP 403 from data-api.binance.vision; often brief: a supervised bot tries again, "
        "by hand start it again"
    )
    body = '{"code":-2015,"msg":"Invalid API-key, IP, or permissions for action."}'
    rejected = httpx.HTTPStatusError(
        "401", request=request, response=httpx.Response(401, text=body, request=request)
    )
    assert runner.start_problem(rejected) == (
        f"HTTP 401 from data-api.binance.vision: {body}; a supervised bot tries again, but if "
        "it keeps failing, check the API key, its IP restriction, and the network"
    )
    busy = httpx.HTTPStatusError(
        "503", request=request, response=httpx.Response(503, text="<html>", request=request)
    )
    assert runner.start_problem(busy) == (
        "HTTP 503 from data-api.binance.vision; often brief: a supervised bot tries again, "
        "by hand start it again"
    )
    assert runner.start_problem(httpx.ConnectError("down", request=request)).startswith(
        "network error ConnectError; often brief"
    )
    assert runner.start_problem(ValueError("no keys")) == "no keys"


def test_a_ledger_keeps_its_mode(tmp_path: Path, offline: tuple[Collect, list[LiveFeed]]) -> None:
    notifier, _ = offline
    config, _, _ = setup_store(tmp_path)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    with Ledger(config.ledger) as ledger:  # a testnet book from before modes were kept
        ledger.add_event(datetime(2026, 10, 1, tzinfo=UTC), "info", "started (testnet), ledger x")

    async def start() -> int:
        stop = asyncio.Event()
        stop.set()  # a session that starts all the same ends at once
        return await run_paper(config, settings, tmp_path / "data", stop=stop)

    with pytest.raises(LedgerModeError, match="keeps a testnet book, not a paper one"):
        asyncio.run(start())
    assert notifier.messages == []  # refused before anything ran

    config.ledger.unlink()

    async def setup(ctx: Context) -> tuple[Executor, list[Coroutine[Any, Any, None]]]:
        raise ValueError("enough")

    asyncio.run(run_session(config, settings, tmp_path / "data", label="paper", setup=setup))
    with Ledger(config.ledger) as ledger:
        assert ledger.get_meta(MODE_META) == "paper"  # a new ledger keeps its mode


@pytest.mark.parametrize(
    "error",
    [
        StartRefused("set the keys"),
        BinanceError(INVALID_SYMBOL, "unknown symbols: ['X']"),
        BinanceError(-1022, "Signature for this request is not valid."),
    ],
)
def test_a_start_that_fails_the_same_way_each_time_ends_for_good(
    tmp_path: Path, offline: tuple[Collect, list[LiveFeed]], error: Exception
) -> None:
    notifier, _ = offline
    config, _, _ = setup_store(tmp_path)

    async def setup(ctx: Context) -> tuple[Executor, list[Coroutine[Any, Any, None]]]:
        raise error

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    code = asyncio.run(run_session(config, settings, tmp_path / "data", label="paper", setup=setup))
    assert code == 4  # the supervisor gives up instead of retrying every 15 minutes
    [message] = notifier.messages
    assert message.startswith(f"[paper] cannot start: {error}")


def test_the_heartbeat_and_stream_alerts_follow_the_feed(
    tmp_path: Path, offline: tuple[Collect, list[LiveFeed]], monkeypatch: pytest.MonkeyPatch
) -> None:
    notifier, feeds = offline
    config, _, _ = setup_store(tmp_path)
    problems: list[Callable[[], str]] = []

    async def heartbeat(url: str, every: float, client: object, problem: Callable[[], str]) -> None:
        problems.append(problem)
        await asyncio.Event().wait()

    monkeypatch.setattr(runner, "heartbeat_loop", heartbeat)
    settings = Settings(_env_file=None, heartbeat_url="https://monitor.example/ping")  # type: ignore[call-arg]

    async def scenario() -> list[str]:
        stop = asyncio.Event()
        session = asyncio.create_task(run_paper(config, settings, tmp_path / "data", stop=stop))
        await until(lambda: bool(feeds) and bool(problems))
        feed, problem = feeds[0], problems[0]
        seen = [problem()]
        feed._stale_reported.add(KEY)
        seen.append(problem())
        assert feed.on_fresh is not None
        feed.on_fresh(KEY)
        await until(lambda: any("is back" in m for m in notifier.messages))
        stop.set()
        await session
        return seen

    assert asyncio.run(scenario()) == ["", "stale streams: BTCUSDT 4h"]
    assert "[paper] stream BTCUSDT 4h is back: bars arrive again" in notifier.messages


def test_the_heartbeat_ignores_a_pair_binance_pauses() -> None:
    class Spot:
        def trading(self, symbol: str) -> bool:
            return symbol != "ETHUSDT"

    spot = cast(BinanceSpot, Spot())
    paused = ("ETHUSDT", H4)
    assert runner.heartbeat_problem([paused], "", spot) == ""
    assert runner.heartbeat_problem([KEY, paused], "", spot) == "stale streams: BTCUSDT 4h"
    assert runner.heartbeat_problem([], "reconciliation failing") == "reconciliation failing"


def test_a_live_session_of_two_quote_assets_ends_for_good(
    tmp_path: Path, offline: tuple[Collect, list[LiveFeed]], monkeypatch: pytest.MonkeyPatch
) -> None:
    notifier, _ = offline
    rules = {**DEFAULT_RULES, "ETHBUSD": DEFAULT_RULES["ETHUSDT"]}
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={"BTCUSDT": 100.0}, rules=rules)

    def spot(*args: Any) -> BinanceSpot:  # config, settings, client, clock, resync
        return BinanceSpot(fake.client(), "key", "secret", base_url=TESTNET_URL, clock=args[3])

    monkeypatch.setattr(runner, "make_spot", spot)
    end = H4.floor(datetime.now(UTC))
    store = BarStore(tmp_path / "data")
    for symbol in ("BTCUSDT", "ETHBUSD"):
        store.write(symbol, H4, price_bars(end - H4.delta * 40, H4, [100.0] * 40, [100.0] * 40))
    config = LiveConfig(
        mode="testnet",
        ownership="account",  # no budget to check first: the setup must see it
        strategies=[
            StrategyConfig(
                name="donchian_trend",
                symbols=["BTCUSDT", "ETHBUSD"],
                timeframe=H4,
                allocation=1.0,
                params={"entry": 5, "exit": 3},
            )
        ],
        ledger=tmp_path / "testnet.sqlite",
        backup_days=0,
    )
    settings = Settings(_env_file=None, binance_api_key="key", binance_api_secret="secret")  # type: ignore[call-arg]
    assert asyncio.run(runner.run_live(config, settings, tmp_path / "data")) == 4
    assert "the symbols are quoted in BUSD, USDT" in notifier.messages[-1]


def test_a_testnet_session_buys_places_its_stop_and_stops_cleanly(
    tmp_path: Path, offline: tuple[Collect, list[LiveFeed]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real-money start path, with the exchange replaced: rules, keys, reconciliation,
    an order at a breakout bar, and the stop that protects it."""
    notifier, feeds = offline
    paper, _, breakout = setup_store(tmp_path)
    fake = FakeSpot(balances={"USDT": 1000.0}, prices={"BTCUSDT": 130.0})

    def spot(*args: Any, **kwargs: Any) -> BinanceSpot:  # config, settings, client, clock
        return BinanceSpot(fake.client(), "key", "secret", base_url=TESTNET_URL, clock=args[3])

    monkeypatch.setattr(runner, "make_spot", spot)
    monkeypatch.setattr(runner, "other_books_cash", lambda config: (0.0, []))  # not config/'s
    config = LiveConfig(
        mode="testnet",
        initial_cash=1000.0,
        strategies=paper.strategies,
        ledger=tmp_path / "testnet.sqlite",
        guard=paper.guard,
        backup_days=0,
    )
    settings = Settings(_env_file=None, binance_api_key="key", binance_api_secret="secret")  # type: ignore[call-arg]
    source = tmp_path / "testnet.yaml"

    async def scenario() -> int:
        stop = asyncio.Event()
        run = runner.run_live(config, settings, tmp_path / "data", stop=stop, source=source)
        session = asyncio.create_task(run)
        await until(lambda: bool(feeds) and any("started" in m for m in notifier.messages))
        feeds[0].queue.put_nowait((KEY, breakout))
        await until(lambda: fake.order_count("STOP_LOSS") > 0)
        stop.set()
        return await session

    assert asyncio.run(scenario()) == 0
    assert notifier.messages[0].startswith("[testnet] started")
    assert any("BUY" in m and "BTCUSDT" in m for m in notifier.messages)
    assert notifier.messages[-1].startswith("[testnet] stopped")
    assert "their exchange stops stay" in notifier.messages[-1]
    assert fake.balances["USDT"] < 1000.0  # the order reached the exchange
    with Ledger(config.ledger) as ledger:
        assert ledger.counts()["fills"] == 1
    [entry] = registered_sessions(tmp_path / "data")  # `tbot halt` finds it by its config
    assert (entry.ledger, entry.config) == (config.ledger.resolve(), source.resolve())


def test_a_budget_changed_at_a_restart_is_booked_before_the_bar_it_hands_over(
    tmp_path: Path, offline: tuple[Collect, list[LiveFeed]], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, store, _ = setup_store(tmp_path)
    close = H4.floor(datetime.now(UTC))
    monkeypatch.setattr(  # what it missed comes from Binance as it would
        "tbot.live.feed.fetch_klines", lambda client, *stream: store.read(*stream)
    )
    with Ledger(config.ledger) as ledger:  # it stopped after the event of the close before
        ledger.set_meta(runner.BUDGET_META, repr(1000.0))
        ledger.add_equity(EquityPoint(close - H4.delta, 1000.0, 1000.0, 0.0))
        checkpoint = {"time": (close - H4.delta).isoformat(), "targets": [{}]}
        checkpoint["strategies"] = runner.strategies_hash(config)
        ledger.set_meta(runner.TARGETS_META, json.dumps(checkpoint))
    monkeypatch.setattr(ServerClock, "now", lambda self: close + timedelta(minutes=5))
    cut = config.model_copy(update={"initial_cash": 400.0})

    async def scenario() -> int:
        stop = asyncio.Event()
        session = asyncio.create_task(
            run_paper(cut, Settings(_env_file=None), tmp_path / "data", stop=stop)  # type: ignore[call-arg]
        )
        # The bar that closed while it was down is traded, not replayed.
        await until(lambda: equity_rows(config.ledger) >= 2)
        stop.set()
        return await session

    assert asyncio.run(scenario()) == 0
    with Ledger(config.ledger) as ledger:
        [moved] = ledger.adjustments()
        first, handed = ledger.equity_points()
    assert first.time < moved.time < handed.time  # the transfer is between the two
    assert handed.equity == pytest.approx(400.0)
    times, equity = [first.time, handed.time], [first.equity, handed.equity]
    assert growth(times, equity, [(moved.time, moved.cash)], 1000.0) == [1.0, 1.0]


def equity_rows(path: Path) -> int:
    with Ledger(path) as ledger:
        return ledger.equity_rows()


def test_a_problem_of_the_feed_reaches_the_owner_and_the_ledger(
    tmp_path: Path, offline: tuple[Collect, list[LiveFeed]]
) -> None:
    notifier, feeds = offline
    config, _, _ = setup_store(tmp_path)
    problem = "no market data socket for 15 min (cannot connect to stream.binance.com:9443)"

    async def scenario() -> None:
        stop = asyncio.Event()
        session = asyncio.create_task(
            run_paper(config, Settings(_env_file=None), tmp_path / "data", stop=stop)  # type: ignore[call-arg]
        )
        await until(lambda: bool(feeds) and any("started" in m for m in notifier.messages))
        assert feeds[0].on_alert is not None
        feeds[0].on_alert(problem)
        await until(lambda: f"[paper] {problem}" in notifier.messages)
        stop.set()
        await session

    asyncio.run(scenario())
    with Ledger(config.ledger) as ledger:
        events = ledger.events_starting(f"[paper] {problem}")
    assert [event.level for event in events] == ["warning"]
