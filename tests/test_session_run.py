"""A whole session, start to stop, with the network replaced: the code tests rarely reach."""

import asyncio
import signal
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest

from factories import price_bars
from tbot.core.config import StrategyConfig
from tbot.core.timeframe import Timeframe
from tbot.data.schema import empty_bars
from tbot.data.store import BarStore
from tbot.exchange.binance import INVALID_SYMBOL, BinanceError
from tbot.execution.sim_broker import SimulatedBroker
from tbot.live import runner
from tbot.live.clock import ServerClock
from tbot.live.config import PaperConfig, Settings
from tbot.live.executor import Executor, PaperExecutor
from tbot.live.feed import LiveFeed
from tbot.live.ledger import Ledger
from tbot.live.runner import (
    MODE_META,
    Context,
    Health,
    LedgerModeError,
    StartRefused,
    reconcile_loop,
    run_paper,
    run_session,
    stop_path,
)
from tbot.risk.guard import GuardConfig

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
        "[live] reconciliation failing (down); new orders wait until it works again",
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
