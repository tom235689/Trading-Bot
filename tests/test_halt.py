"""The panic button: `tbot halt` and Telegram /halt sell what the bot holds and halt."""

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from binance_spot_fake import FakeSpot
from factories import Scripted, price_bars
from tbot.cli import EXIT_CONFIG, EXIT_ERROR, main
from tbot.core.config import StrategyConfig
from tbot.core.models import Fill
from tbot.core.timeframe import Timeframe
from tbot.exchange.binance import TESTNET_URL, BinanceSpot
from tbot.execution.rebalance import RebalanceRules
from tbot.execution.sim_broker import CostModel, SimulatedBroker
from tbot.live.config import LiveConfig, PaperConfig, named_ledger
from tbot.live.executor import LiveExecutor, PaperExecutor
from tbot.live.history import BarHistory
from tbot.live.ledger import Ledger
from tbot.live.runner import (
    GUARD_META,
    HALT_BY_HAND,
    HALT_BY_TELEGRAM,
    SUPERVISED_META,
    SessionTrader,
    command_answer,
    halt_path,
    halt_retry_loop,
    halt_targets,
    held_text,
    instance_lock,
    load_guard,
    register_session,
    resume,
    running_ledger,
    watch_halt_request,
)
from tbot.live.session import TradingSession
from tbot.portfolio.allocation import StrategySlot
from tbot.portfolio.portfolio import Portfolio
from tbot.risk.guard import Decision, GuardConfig, GuardState, Mode, RiskGuard

H1 = Timeframe.H1
T0 = datetime(2024, 1, 1, tzinfo=UTC)
BTC = ("BTC", H1)
NO_GUARD = GuardConfig(daily_loss_limit=0, max_drawdown=0, stale_seconds=0)
STRATEGY = "  - {name: donchian_trend, symbols: [BTCUSDT], timeframe: 4h, allocation: 1.0}\n"


def at(hours: int) -> datetime:
    return T0 + timedelta(hours=hours)


class Prices:
    """A book price of 100; none while the exchange is down."""

    def __init__(self) -> None:
        self.down = False

    async def price(self, symbol: str, side: int) -> float:
        if self.down:
            raise OSError("no route to the exchange")
        return 100.0


class Collect:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


def paper_trader(
    tmp_path: Path, prices: Prices | None = None, minimum: float = 0.0
) -> tuple[SessionTrader, Ledger, Collect]:
    config = PaperConfig(
        initial_cash=1000.0,
        costs=CostModel(fee_rate=0.001, slippage_bps=0),
        rebalance=RebalanceRules(min_notional=minimum, rebalance_threshold=0),
        strategies=[StrategyConfig(name="scripted", symbols=["BTC"], timeframe=H1, allocation=1)],
        ledger=tmp_path / "paper.sqlite",
        guard=NO_GUARD,
    )
    session = TradingSession(
        config,
        [StrategySlot(Scripted(["BTC"], {"script": {at(1): {"BTC": 1.0}}}), H1, 1.0)],
        {BTC: BarHistory(H1)},
        Portfolio(config.initial_cash),
    )
    ledger, notifier = Ledger(config.ledger), Collect()
    broker = SimulatedBroker(config.costs)
    executor = PaperExecutor(broker, prices or Prices(), ledger, notifier, lambda: at(9))
    trader = SessionTrader(
        session, ledger, executor, notifier, RiskGuard(NO_GUARD), clock=lambda: at(9)
    )
    return trader, ledger, notifier


def config_of(trader: SessionTrader) -> PaperConfig:
    config = trader.session.config
    assert isinstance(config, PaperConfig)
    return config


def bar(trader: SessionTrader, index: int) -> list[Fill]:
    return asyncio.run(trader.handle({BTC: price_bars(at(index), H1, [100.0], [100.0])}))


def test_a_halt_sells_now_and_trades_no_more_until_resumed(tmp_path: Path) -> None:
    trader, ledger, notifier = paper_trader(tmp_path)
    assert len(bar(trader, 0)) == 1  # bought
    fills = asyncio.run(trader.halt_now(HALT_BY_HAND))
    assert [f.quantity < 0 for f in fills] == [True]  # sold at once, not at the next bar
    assert trader.session.portfolio.positions == {}
    state = load_guard(config_of(trader), ledger).state
    assert state.halted
    assert state.halt_reason == HALT_BY_HAND
    assert any(m.startswith("[paper] KILL SWITCH: halted by hand") for m in notifier.messages)

    sent = len(notifier.messages)
    assert asyncio.run(trader.halt_now(HALT_BY_TELEGRAM)) == []  # nothing left to sell
    assert len(notifier.messages) == sent  # and nothing announced twice
    assert load_guard(config_of(trader), ledger).state.halt_reason == HALT_BY_HAND
    assert bar(trader, 1) == []  # the strategy wants to buy: halted
    assert resume(config_of(trader)).startswith("resumed")
    assert len(bar(trader, 2)) == 1
    ledger.close()


def test_telegram_halt_needs_the_code_of_its_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trader, ledger, _ = paper_trader(tmp_path)
    bar(trader, 0)
    answer = command_answer(
        trader.session, ledger, "paper", lambda: at(1), list, halt=trader.halt_now
    )
    clock = [0.0]
    monkeypatch.setattr("tbot.live.runner.time.monotonic", lambda: clock[0])

    def ask(text: str) -> str:
        return asyncio.run(answer(text))

    first = ask("/halt")  # a tap on /halt in the help text
    assert "To go on, send /halt " in first
    code = first.split("send /halt ")[1].split()[0]
    assert len(code) == 4
    assert code.isdigit()
    wrong = "0000" if code != "0000" else "1111"
    assert ask(f"/halt {wrong}").startswith("[paper] wrong or expired code, nothing done")
    assert ask(f"/halt {code}").startswith("[paper] wrong or expired")  # one try per code
    code = ask("/halt").split("send /halt ")[1].split()[0]
    clock[0] = 121.0
    assert ask(f"/halt {code}").startswith("[paper] wrong or expired")  # too late
    assert trader.session.portfolio.positions  # nothing sold yet

    code = ask("/Halt@my_bot").split("send /halt ")[1].split()[0]  # as sent in a group
    done = ask(f"/halt@my_bot {code}")
    assert done.startswith("[paper] halted: 1 fills, nothing held")
    assert load_guard(config_of(trader), ledger).state.halt_reason == HALT_BY_TELEGRAM
    ledger.close()


def test_telegram_halt_halts_the_other_sessions_too(tmp_path: Path) -> None:
    trader, ledger, _ = paper_trader(tmp_path)
    other = tmp_path / "live.sqlite"

    async def halt(reason: str) -> list[Fill]:
        assert halt_path(other).exists()  # asked first: both sell at once
        return []

    stopped = tmp_path / "eth.sqlite"  # between a supervisor's restarts
    others = [("live", other, True), ("eth", stopped, False)]
    answer = command_answer(
        trader.session, ledger, "paper", lambda: at(1), list, None, halt, "paper", lambda: others
    )
    code = asyncio.run(answer("/halt")).split("send /halt ")[1].split()[0]
    reply = asyncio.run(answer(f"/halt {code}"))
    assert reply.endswith(
        "Also halting: live; their alerts follow. Halting at their next start: eth."
    )
    assert halt_path(stopped).exists()
    assert "`tbot resume paper`" in reply
    seen: list[str] = []

    async def record(reason: str) -> None:
        seen.append(reason)

    async def watch() -> None:
        task = asyncio.create_task(watch_halt_request(halt_path(other), record))
        async with asyncio.timeout(5):
            while halt_path(other).exists():
                await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(watch())
    assert seen == [HALT_BY_TELEGRAM]  # the other session says who asked
    ledger.close()


def test_the_sessions_a_halt_reaches_are_found_by_their_configs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    for name, body in {
        "paper": f"ledger: paper.sqlite\nstrategies:\n{STRATEGY}",
        "live": f"mode: live\ninitial_cash: 500\nledger: live.sqlite\nstrategies:\n{STRATEGY}",
        "typo": f"ledger: typo.sqlite\ninitial_cahs: 5\nstrategies:\n{STRATEGY}",
        "idle": f"ledger: idle.sqlite\nstrategies:\n{STRATEGY}",
        "sup": f"ledger: sup.sqlite\nstrategies:\n{STRATEGY}",
        "nameless": f"mode: testnet\ninitial_cahs: 5\nstrategies:\n{STRATEGY}",
        "backtest": f"start: 2024-01-01\nstrategies:\n{STRATEGY}",
    }.items():
        (tmp_path / "config" / f"{name}.yaml").write_text(body, "utf-8")
    (tmp_path / "data").mkdir()
    for ledger in ("paper", "live", "typo", "idle", "sup", "data/testnet"):
        Ledger(tmp_path / f"{ledger}.sqlite").close()
    with Ledger(tmp_path / "sup.sqlite") as book:
        book.set_meta(SUPERVISED_META, "1")  # its supervisor may start it again
    with (
        instance_lock(tmp_path / "paper.sqlite"),
        instance_lock(tmp_path / "live.sqlite"),
        instance_lock(tmp_path / "typo.sqlite"),
        instance_lock(tmp_path / "data" / "testnet.sqlite"),
    ):
        found = halt_targets(tmp_path / "paper.sqlite")
    assert [(name, running) for name, _, running in found] == [
        ("live", True),
        ("nameless", True),  # broken, on the default ledger of its mode
        ("sup", False),
        ("typo", True),  # broken, on the ledger it names
    ]


def test_a_config_that_does_not_load_still_names_its_ledger() -> None:
    assert named_ledger({"mode": "testnet", "initial_cahs": 5}) == Path("data/testnet.sqlite")
    assert named_ledger({"mode": "live"}) == Path("data/live.sqlite")
    assert named_ledger({"strategies": []}) == Path("data/paper.sqlite")
    assert named_ledger({"mode": "live", "ledger": "x.sqlite"}) == Path("x.sqlite")
    assert named_ledger({"ledger": 5}) is None
    assert named_ledger({"mode": "futures"}) is None


def test_a_start_after_a_resume_sells_nothing(tmp_path: Path) -> None:
    trader, ledger, _ = paper_trader(tmp_path)
    bar(trader, 0)
    trader.guard.halt(HALT_BY_HAND)
    trader.save_guard()
    resume(config_of(trader))  # while the session was starting
    assert asyncio.run(trader.sell_if_halted()) == []
    assert trader.session.portfolio.positions
    ledger.close()


def test_a_halt_whose_sale_failed_sells_again_within_minutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tbot.live.runner.HALT_RETRY_SECONDS", 0.01)
    prices = Prices()
    trader, ledger, notifier = paper_trader(tmp_path, prices)
    bar(trader, 0)
    prices.down = True
    assert asyncio.run(trader.halt_now(HALT_BY_HAND)) == []  # not at the next bar, hours away
    assert "1 NOT SOLD" in notifier.messages[-1]
    prices.down = False

    async def wait() -> None:
        task = asyncio.create_task(halt_retry_loop(trader))
        async with asyncio.timeout(5):
            while trader.session.portfolio.positions:
                await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(wait())
    assert "Positions flattened (1 fills)" in notifier.messages[-1]
    ledger.close()


def test_dust_is_neither_sold_nor_called_a_holding(tmp_path: Path) -> None:
    trader, ledger, notifier = paper_trader(tmp_path, minimum=50.0)
    portfolio, marks = trader.session.portfolio, trader.session.marks
    portfolio.positions["BTC"] = 0.1  # 10.00 at 100: no order can sell it
    marks["BTC"] = 100.0
    assert trader.sellable() == {}
    assert asyncio.run(trader.halt_now(HALT_BY_HAND)) == []
    assert ledger.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert "1 left below the exchange minimum" in notifier.messages[-1]
    assert held_text(portfolio, marks, 50.0) == "nothing held"
    portfolio.positions["BTC"] = 1.0
    assert held_text(portfolio, marks, 50.0) == "still held: BTC 1.000000 (100.00)"
    ledger.close()


def test_a_resume_withdraws_a_request_of_a_book_that_is_not_halted(tmp_path: Path) -> None:
    trader, ledger, _ = paper_trader(tmp_path)
    request = halt_path(config_of(trader).ledger)
    request.write_text("telegram\n", encoding="utf-8")  # its session stopped before taking it
    assert resume(config_of(trader)).startswith("not halted; withdrew")
    assert not request.exists()  # never sells at a later start
    assert resume(config_of(trader)) == "not halted"
    ledger.close()


def test_resume_without_a_name_finds_a_waiting_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "paper.yaml").write_text(
        f"ledger: paper.sqlite\nstrategies:\n{STRATEGY}", "utf-8"
    )
    Ledger(tmp_path / "paper.sqlite").close()
    request = halt_path(tmp_path / "paper.sqlite")
    request.write_text("halt\n", encoding="utf-8")
    assert main(["status", "paper"]) == 0
    assert "a `tbot halt` request waits" in capsys.readouterr().out
    assert main(["resume"]) == 0
    assert "withdrew a `tbot halt` request" in capsys.readouterr().out
    assert not request.exists()


def test_a_resume_withdraws_a_halt_request_no_session_took(tmp_path: Path) -> None:
    trader, ledger, _ = paper_trader(tmp_path)
    trader.guard.halt(HALT_BY_HAND)
    trader.save_guard()
    request = halt_path(config_of(trader).ledger)
    request.write_text("halt\n", encoding="utf-8")
    assert resume(config_of(trader)).startswith("resumed")
    assert not request.exists()  # never sells at a later start
    ledger.close()


def test_halt_reaches_a_running_session_whose_config_no_longer_loads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = session_file(tmp_path, "")
    ledger_path = tmp_path / "s.sqlite"
    Ledger(ledger_path).close()
    config.write_text(config.read_text("utf-8") + "initial_cahs: 5\n", "utf-8")  # edited
    request = halt_path(ledger_path)
    held, done = threading.Event(), threading.Event()

    def session() -> None:
        with instance_lock(ledger_path):
            held.set()
            while not request.exists():
                if done.wait(0.05):
                    return
            request.unlink()
            done.wait(5)

    thread = threading.Thread(target=session)
    thread.start()
    try:
        held.wait(5)
        assert main(["halt", str(config), "--timeout", "10"]) == 0
    finally:
        done.set()
        thread.join(5)
    assert "halted; `tbot status" in capsys.readouterr().out
    assert main(["halt", str(config)]) == EXIT_CONFIG  # not running: the config must load


def test_telegram_halt_that_fails_says_how_to_get_out(tmp_path: Path) -> None:
    trader, ledger, _ = paper_trader(tmp_path)

    async def broken(reason: str) -> list[Fill]:
        raise OSError("disk full")

    answer = command_answer(trader.session, ledger, "paper", lambda: at(1), list, halt=broken)
    code = asyncio.run(answer("/halt")).split("send /halt ")[1].split()[0]
    reply = asyncio.run(answer(f"/halt {code}"))
    assert "halt FAILED" in reply
    assert "run `tbot halt` on the computer" in reply
    assert "Binance" not in reply  # a sale by hand would not leave the book
    ledger.close()


def test_a_halt_request_is_removed_only_once_the_sales_are_done(tmp_path: Path) -> None:
    request = halt_path(tmp_path / "paper.sqlite")
    request.write_text("halt\n", encoding="utf-8")
    seen: list[bool] = []

    async def halt(reason: str) -> None:
        seen.append(request.exists())  # a crash now leaves it for the next start

    async def run() -> None:
        task = asyncio.create_task(watch_halt_request(request, halt))
        async with asyncio.timeout(5):
            while request.exists():
                await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert seen == [True]


def test_a_halted_book_that_holds_coins_sells_them_at_the_start(tmp_path: Path) -> None:
    trader, ledger, _ = paper_trader(tmp_path)
    bar(trader, 0)
    assert asyncio.run(trader.sell_if_halted()) == []  # not halted: it keeps its position
    trader.guard.halt(HALT_BY_HAND)  # `tbot halt` while it was stopped
    trader.save_guard()
    assert len(asyncio.run(trader.sell_if_halted())) == 1
    assert trader.session.portfolio.positions == {}
    assert asyncio.run(trader.sell_if_halted()) == []
    ledger.close()


def session_file(tmp_path: Path, body: str) -> Path:
    config = tmp_path / "s.yaml"
    config.write_text(
        f"{body}ledger: {(tmp_path / 's.sqlite').as_posix()}\nstrategies:\n{STRATEGY}", "utf-8"
    )
    return config


def test_halt_marks_a_stopped_session_and_names_what_its_start_sells(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = session_file(tmp_path, "mode: testnet\n")
    with Ledger(tmp_path / "s.sqlite") as ledger:
        ledger.add_fill(Fill(T0, "BTCUSDT", 0.01, 50000.0, 0.5), 50000.0)
    assert main(["halt", str(config)]) == 0
    out = capsys.readouterr().out
    assert "(no session was running): its next start sells BTCUSDT 0.010000 at once" in out
    assert "the exchange stops stay in place; to sell now, start it" in out
    with Ledger(tmp_path / "s.sqlite") as ledger:
        state = GuardState.model_validate_json(ledger.get_meta(GUARD_META) or "")
    assert state.halted
    assert state.halt_reason == HALT_BY_HAND

    assert main(["halt", str(config)]) == 0
    assert "was halted already (halted by hand" in capsys.readouterr().out


def halt_running(ledger_path: Path, args: list[str]) -> int:
    """`tbot halt` against a session that halts when asked, and sells nothing."""
    request = halt_path(ledger_path)
    held, done = threading.Event(), threading.Event()

    def session() -> None:
        with instance_lock(ledger_path):
            held.set()
            while not request.exists():
                if done.wait(0.05):
                    return
            with Ledger(ledger_path) as ledger:
                state = GuardState(halted=True, halt_reason=HALT_BY_HAND)
                ledger.set_meta(GUARD_META, state.model_dump_json())
            request.unlink()
            done.wait(5)

    thread = threading.Thread(target=session)
    thread.start()
    try:
        held.wait(5)
        return main(["halt", *args, "--timeout", "10"])
    finally:
        done.set()
        thread.join(5)


def test_halt_says_what_a_failed_sale_left(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = session_file(tmp_path, "")
    with Ledger(tmp_path / "s.sqlite") as ledger:
        ledger.add_fill(Fill(T0, "BTCUSDT", 0.01, 50000.0, 0.5), 50000.0)
        ledger.add_fill(Fill(T0, "ETHUSDT", 0.001, 3000.0, 0.0), 3000.0)  # 3.00: dust
    assert halt_running(tmp_path / "s.sqlite", [str(config)]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "halted, but not sold yet: BTCUSDT 0.010000 (see the alerts)" in err
    assert "ETH" not in err


def test_halt_without_a_name_takes_the_running_session_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "paper.yaml").write_text(
        f"ledger: paper.sqlite\nstrategies:\n{STRATEGY}", "utf-8"
    )
    (tmp_path / "config" / "testnet.yaml").write_text(  # edited into errors while it runs
        f"mode: testnet\ninitial_cahs: 5\nstrategies:\n{STRATEGY}", "utf-8"
    )
    (tmp_path / "data").mkdir()
    Ledger(tmp_path / "paper.sqlite").close()
    Ledger(tmp_path / "data" / "testnet.sqlite").close()
    assert halt_running(tmp_path / "data" / "testnet.sqlite", []) == 0
    out = capsys.readouterr().out
    assert "testnet.yaml" in out
    assert "halted; `tbot status testnet` shows what it holds" in out
    with Ledger(tmp_path / "paper.sqlite") as ledger:
        assert ledger.get_meta(GUARD_META) is None  # the stopped book is left alone


def test_halt_reaches_a_running_session(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = session_file(tmp_path, "")
    Ledger(tmp_path / "s.sqlite").close()
    assert halt_running(tmp_path / "s.sqlite", [str(config)]) == 0
    assert "halted: the bot holds nothing; `tbot resume" in capsys.readouterr().out


def test_halt_never_picks_a_real_money_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "live.yaml").write_text(
        f"mode: live\ninitial_cash: 1000\nledger: live.sqlite\nstrategies:\n{STRATEGY}", "utf-8"
    )
    Ledger(tmp_path / "live.sqlite").close()
    assert main(["halt"]) == EXIT_CONFIG
    assert "name it to sell and halt it: tbot halt live" in capsys.readouterr().err
    assert main(["halt", "live"]) == 0
    assert "halted live (no session was running)" in capsys.readouterr().out


def live_halt(tmp_path: Path, fake: FakeSpot, *, stop_sold: bool) -> tuple[float, int, list[str]]:
    """A testnet session holding 0.01 BTC with its stop in place halts by hand; the stop may
    have sold just before."""
    symbol = "BTCUSDT"
    config = LiveConfig.model_validate(
        {
            "mode": "testnet",
            "initial_cash": 1000.0,
            "strategies": [
                StrategyConfig(
                    name="donchian_trend", symbols=[symbol], timeframe=H1, allocation=1.0
                )
            ],
            "ledger": tmp_path / "live.sqlite",
        }
    )

    async def go() -> tuple[float, int, list[str]]:
        async with fake.client() as client:
            spot = BinanceSpot(client, fake.api_key, fake.secret, base_url=TESTNET_URL)
            await spot.load_rules([symbol])
            ledger, notifier = Ledger(config.ledger), Collect()
            executor = LiveExecutor(
                spot, ledger, notifier, lambda: T0, fee_rate=0.001, protective_stop_pct=0.2
            )
            portfolio = Portfolio(500.0)
            portfolio.positions[symbol] = 0.01
            session = TradingSession(
                config,
                [StrategySlot(Scripted([symbol]), H1, 1.0)],
                {(symbol, H1): BarHistory(H1)},
                portfolio,
            )
            session.marks[symbol] = 50000.0
            await executor.after_event(portfolio, session.marks)  # the stop in place
            if stop_sold:
                fake.trigger_stop(symbol)
            trader = SessionTrader(
                session, ledger, executor, notifier, RiskGuard(GuardConfig()), lambda: T0, "testnet"
            )
            try:
                await trader.halt_now(HALT_BY_HAND)
                skipped = ledger.conn.execute(
                    "SELECT COUNT(*) FROM orders WHERE status = 'skipped'"
                ).fetchone()[0]
            finally:
                ledger.close()
            return portfolio.positions.get(symbol, 0.0), int(skipped), notifier.messages

    return asyncio.run(go())


def test_a_live_halt_books_a_stop_that_sold_before_it_plans_a_sale(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 500.0, "BTC": 0.01}, prices={"BTCUSDT": 50000.0})
    left, skipped, messages = live_halt(tmp_path, fake, stop_sold=True)
    assert left == 0.0
    assert skipped == 0  # nothing left to sell: no order planned for coins already gone
    assert any("protective stop sold" in m for m in messages)
    assert any("KILL SWITCH: halted by hand" in m for m in messages)


def test_a_live_halt_cancels_the_stop_before_it_sells(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 500.0, "BTC": 0.01}, prices={"BTCUSDT": 50000.0})
    left, skipped, messages = live_halt(tmp_path, fake, stop_sold=False)
    assert left == 0.0
    assert skipped == 0
    stops = sum(1 for o in fake.orders if o["type"] == "STOP_LOSS" and o["status"] == "NEW")
    assert stops == 0  # cancelled, so it locks nothing and never sells again
    assert fake.balances["BTC"] == pytest.approx(0.0)
    assert any("KILL SWITCH: halted by hand" in m for m in messages)


def test_a_halt_reaches_sessions_whose_config_was_broken_or_moved_while_they_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    broken = tmp_path / "config" / "broken.yaml"
    moved = tmp_path / "config" / "moved.yaml"
    broken.write_text("ledger: broken.sqlite\nstrategies:\n\t- tab\n", "utf-8")  # not YAML now
    moved.write_text(f"ledger: new.sqlite\nstrategies:\n{STRATEGY}", "utf-8")  # ran on old
    data = tmp_path / "data"
    for ledger, source in (("broken", broken), ("old", moved)):
        Ledger(tmp_path / f"{ledger}.sqlite").close()
        register_session(data, tmp_path / f"{ledger}.sqlite", source, source.stem)
    with instance_lock(tmp_path / "broken.sqlite"), instance_lock(tmp_path / "old.sqlite"):
        found = halt_targets(tmp_path / "own.sqlite", data)
        assert running_ledger(data, moved) == (tmp_path / "old.sqlite").resolve()
    assert sorted((name, running) for name, _, running in found) == [
        ("broken", True),
        ("moved", True),
    ]


def test_a_halt_reaches_a_stopped_book_that_holds_coins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    for name in ("holds", "empty", "dust"):
        body = f"ledger: {name}.sqlite\nstrategies:\n{STRATEGY}"
        (tmp_path / "config" / f"{name}.yaml").write_text(body, "utf-8")
    with Ledger(tmp_path / "holds.sqlite") as book:  # started by hand, stopped holding BTC
        book.add_fill(Fill(T0, "BTCUSDT", 0.5, 100.0, 0.0), 100.0)
    with Ledger(tmp_path / "dust.sqlite") as book:
        book.add_fill(Fill(T0, "BTCUSDT", 0.01, 100.0, 0.0), 100.0)  # 1.00: below any order
    Ledger(tmp_path / "empty.sqlite").close()
    found = halt_targets(tmp_path / "own.sqlite", tmp_path / "data")
    assert [(name, running) for name, _, running in found] == [("holds", False)]


def test_telegram_halt_names_a_session_it_could_not_reach(tmp_path: Path) -> None:
    trader, ledger, _ = paper_trader(tmp_path)

    async def halt(reason: str) -> list[Fill]:
        return []

    gone = tmp_path / "missing" / "eth.sqlite"  # its folder is gone: no request can go there
    answer = command_answer(
        trader.session,
        ledger,
        "paper",
        lambda: at(1),
        list,
        None,
        halt,
        "paper",
        lambda: [("eth", gone, True)],
    )
    code = asyncio.run(answer("/halt")).split("send /halt ")[1].split()[0]
    reply = asyncio.run(answer(f"/halt {code}"))
    assert reply.endswith("NOT REACHED: eth; on the computer: `tbot halt eth`")
    assert "Also halting" not in reply
    ledger.close()


def test_a_sale_that_keeps_failing_is_told_once_an_hour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tbot.live.runner.HALT_RETRY_SECONDS", 0.01)
    monkeypatch.setattr("tbot.live.runner.HALT_RETRY_MAX_SECONDS", 0.01)
    prices = Prices()
    trader, ledger, notifier = paper_trader(tmp_path, prices)
    bar(trader, 0)
    prices.down = True
    asyncio.run(trader.halt_now(HALT_BY_HAND))

    async def retries() -> None:
        task = asyncio.create_task(halt_retry_loop(trader))
        await asyncio.sleep(0.3)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(retries())
    failed = ledger.conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'failed'")
    assert failed.fetchone()[0] > 2  # tried again and again
    told = [m for m in notifier.messages if "price lookup failed" in m]
    assert len(told) == 1
    assert told[0].endswith("(more failures of BTC this hour go to the log)")
    ledger.close()


def test_entries_a_daily_loss_limit_blocks_are_told_once_a_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trader, ledger, notifier = paper_trader(tmp_path)
    blocked = Decision(Mode.REDUCE_ONLY, "day loss -5.0%")
    monkeypatch.setattr(trader.guard, "check", lambda *args: blocked)
    for index in (0, 1, 2, 3, 24):  # the strategy wants to buy at each of these closes
        bar(trader, index)
    told = [m for m in notifier.messages if "entries blocked" in m]
    assert (
        told
        == [
            "[paper] entries blocked: day loss -5.0%; more today are logged, not sent",
        ]
        * 2
    )  # the first day, then the next
    ledger.close()


def test_tbot_halt_reaches_a_session_whose_config_now_names_another_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    config = tmp_path / "config" / "moved.yaml"
    config.write_text(f"ledger: new.sqlite\nstrategies:\n{STRATEGY}", "utf-8")  # edited
    old = tmp_path / "old.sqlite"
    Ledger(old).close()
    register_session(tmp_path / "data", old, config, "moved")  # it started on old.sqlite
    held, done = threading.Event(), threading.Event()

    def session() -> None:
        with instance_lock(old):
            held.set()
            while not halt_path(old).exists():
                if done.wait(0.05):
                    return
            with Ledger(old) as book:  # sold nothing, held nothing: halted
                state = GuardState(halted=True, halt_reason=HALT_BY_HAND)
                book.set_meta(GUARD_META, state.model_dump_json())
            halt_path(old).unlink()
            done.wait(5)

    thread = threading.Thread(target=session)
    thread.start()
    try:
        held.wait(5)
        assert main(["halt", "moved", "--timeout", "10"]) == 0
    finally:
        done.set()
        thread.join(5)
    assert f"asked the session on {old.resolve()}" in capsys.readouterr().out
    assert not (tmp_path / "new.sqlite").exists()
