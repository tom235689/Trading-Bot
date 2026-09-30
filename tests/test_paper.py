import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from factories import Scripted, price_bars
from tbot.cli import load_session_config, main
from tbot.core.config import StrategyConfig
from tbot.core.timeframe import Timeframe
from tbot.data.store import BarStore
from tbot.execution.rebalance import RebalanceRules
from tbot.execution.sim_broker import CostModel, SimulatedBroker
from tbot.live.config import LiveConfig, PaperConfig, Settings, load_paper_config
from tbot.live.executor import PaperExecutor
from tbot.live.guard import GuardConfig, RiskGuard
from tbot.live.history import BarHistory
from tbot.live.ledger import Ledger
from tbot.live.runner import (
    SessionTrader,
    build_session,
    lookback_bars,
    make_notifier,
    restore_portfolio,
    status_text,
    stream_keys,
    summary_text,
)
from tbot.live.session import TradingSession
from tbot.monitoring.telegram import LogNotifier, Telegram
from tbot.portfolio.allocation import StrategySlot, build_slots
from tbot.portfolio.portfolio import Portfolio
from tbot.risk.limits import RiskLimits

H1, H4 = Timeframe.H1, Timeframe.H4
T0 = datetime(2024, 1, 1, tzinfo=UTC)
BTC = ("BTC", H1)
NO_GUARD = GuardConfig(daily_loss_limit=0, max_drawdown=0, stale_seconds=0)


def at(hours: int) -> datetime:
    return T0 + timedelta(hours=hours)


class FakePrices:
    def __init__(self, prices: dict[str, float]) -> None:
        self.prices = prices

    async def price(self, symbol: str, side: int) -> float:
        return self.prices[symbol]


class FailingPrices:
    async def price(self, symbol: str, side: int) -> float:
        raise httpx.ConnectError("offline")


class Collect:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


def paper_config(tmp_path: Path) -> PaperConfig:
    return PaperConfig(
        initial_cash=1000.0,
        costs=CostModel(fee_rate=0.001, slippage_bps=0),
        rebalance=RebalanceRules(min_notional=0, rebalance_threshold=0),
        strategies=[StrategyConfig(name="scripted", symbols=["BTC"], timeframe=H1, allocation=1)],
        ledger=tmp_path / "paper.sqlite",
        guard=NO_GUARD,
    )


def make_trader(
    tmp_path: Path, script: dict[datetime, dict[str, float]], prices: FakePrices | FailingPrices
) -> tuple[SessionTrader, Ledger, Collect]:
    config = paper_config(tmp_path)
    session = TradingSession(
        config,
        [StrategySlot(Scripted(["BTC"], {"script": script}), H1, 1.0)],
        {BTC: BarHistory(H1)},
        Portfolio(config.initial_cash),
    )
    ledger = Ledger(config.ledger)
    notifier = Collect()
    executor = PaperExecutor(SimulatedBroker(config.costs), prices, ledger, notifier, lambda: at(9))
    trader = SessionTrader(
        session, ledger, executor, notifier, RiskGuard(NO_GUARD), clock=lambda: at(9)
    )
    return trader, ledger, notifier


BARS = price_bars(T0, H1, [100.0, 100.0, 100.0], [100.0, 100.0, 100.0])


def test_handle_fills_records_and_alerts(tmp_path: Path) -> None:
    script = {at(1): {"BTC": 0.5}, at(2): {"BTC": 0.0}}
    trader, ledger, notifier = make_trader(tmp_path, script, FakePrices({"BTC": 100.0}))

    buys = asyncio.run(trader.handle({BTC: BARS.slice(0, 1)}))
    sells = asyncio.run(trader.handle({BTC: BARS.slice(1, 1)}))
    nothing = asyncio.run(trader.handle({BTC: BARS.slice(2, 1)}))

    assert [f.quantity for f in buys] == [pytest.approx(5.0)]
    assert [f.quantity for f in sells] == [pytest.approx(-5.0)]
    assert nothing == []
    assert buys[0].time == at(9)  # fill time is the wall clock, not the bar close
    assert ledger.fills() == buys + sells
    counts = ledger.counts()
    assert (counts["fills"], counts["orders"], counts["signals"], counts["equity"]) == (2, 2, 3, 3)
    latest = ledger.latest_equity()
    assert latest is not None
    assert latest.equity == pytest.approx(1000 - 0.5 - 0.5)  # two fees, flat price
    assert latest.time == at(3)
    assert notifier.messages[0].startswith("[paper] BUY 5.000000 BTC @ 100.00")
    assert notifier.messages[1].startswith("[paper] SELL 5.000000 BTC @ 100.00")
    assert ledger.get_meta("guard") is not None  # guard state persisted every event
    ledger.close()


def test_price_failure_skips_only_that_order(tmp_path: Path) -> None:
    trader, ledger, notifier = make_trader(tmp_path, {at(1): {"BTC": 0.5}}, FailingPrices())
    fills = asyncio.run(trader.handle({BTC: BARS.slice(0, 1)}))
    assert fills == []
    assert ledger.counts()["orders"] == 1
    assert ledger.counts()["fills"] == 0
    assert "price lookup failed for BTC" in notifier.messages[0]
    ledger.close()


def test_restore_portfolio_replays_ledger_fills(tmp_path: Path) -> None:
    trader, ledger, _ = make_trader(tmp_path, {at(1): {"BTC": 0.5}}, FakePrices({"BTC": 100.0}))
    asyncio.run(trader.handle({BTC: BARS.slice(0, 1)}))
    live = trader.session.portfolio
    ledger.close()

    restored = restore_portfolio(paper_config(tmp_path), Ledger(paper_config(tmp_path).ledger))
    assert restored.cash == pytest.approx(live.cash)
    assert restored.positions == {"BTC": pytest.approx(5.0)}


def test_summary_and_status_text(tmp_path: Path) -> None:
    trader, ledger, _ = make_trader(tmp_path, {at(1): {"BTC": 0.5}}, FakePrices({"BTC": 100.0}))
    asyncio.run(trader.handle({BTC: BARS.slice(0, 1)}))
    summary = summary_text(trader.session, ledger, at(1), "paper")
    assert "equity 999.50" in summary
    assert "BTC 5.000000" in summary
    ledger.close()

    store = BarStore(tmp_path / "data")
    store.write("BTC", H1, BARS)
    status = status_text(paper_config(tmp_path), store)
    assert "fills 1, round trips 0, adjustments 0" in status
    assert "position BTC 5.000000" in status
    assert "last stored bar BTC: 2024-01-01 02:00" in status
    assert "HALTED" not in status


def test_stream_keys_and_lookback() -> None:
    config = PaperConfig(
        strategies=[
            StrategyConfig(
                name="donchian_trend",
                symbols=["BTC/USDT"],
                timeframe=H4,
                allocation=0.5,
                params={"entry": 5, "exit": 3},
            ),
            StrategyConfig(
                name="donchian_trend",
                symbols=["ETH/USDT"],
                timeframe=H1,
                allocation=0.5,
                params={"entry": 10, "exit": 3},
            ),
        ]
    )
    assert stream_keys(config) == [("ETHUSDT", H1), ("BTCUSDT", H4)]
    slots = build_slots(config.strategies)
    assert lookback_bars(slots, config.risk) == {("BTCUSDT", H4): 12, ("ETHUSDT", H1): 22}
    # Volatility targeting needs its window on the finest stream of each symbol.
    vol = RiskLimits(target_volatility=0.4, volatility_lookback_days=1)
    assert lookback_bars(slots, vol) == {("BTCUSDT", H4): 14, ("ETHUSDT", H1): 50}


def test_build_session_needs_stored_history(tmp_path: Path) -> None:
    config = PaperConfig(
        strategies=[
            StrategyConfig(
                name="donchian_trend",
                symbols=["BTCUSDT"],
                timeframe=H4,
                allocation=1.0,
                params={"entry": 5, "exit": 3},
            )
        ]
    )
    store = BarStore(tmp_path / "data")
    now = T0 + timedelta(hours=4 * 30, minutes=10)
    with pytest.raises(ValueError, match="not enough stored bars"):
        build_session(config, store, Portfolio(1000.0), now)

    closes = [100.0 + i for i in range(30)]
    store.write("BTCUSDT", H4, price_bars(T0, H4, [closes[0], *closes[:-1]], closes))
    session = build_session(config, store, Portfolio(1000.0), now)
    assert len(session.histories[("BTCUSDT", H4)]) == 12
    assert session.marks == {"BTCUSDT": 129.0}
    assert session.slots[0].targets == {"BTCUSDT": 1.0}  # rising series: long after warmup


def test_configs_and_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paper = load_paper_config(Path("config/paper.yaml"))
    assert paper.ledger == Path("data/paper.sqlite")
    assert paper.risk.max_symbol_weight == 0.5
    testnet = load_session_config(Path("config/testnet.yaml"))
    assert isinstance(testnet, LiveConfig)
    assert testnet.mode == "testnet"
    live = load_session_config(Path("config/live.yaml"))
    assert isinstance(live, LiveConfig)
    assert (live.mode, live.protective_stop_pct, live.guard.max_drawdown) == ("live", 0.2, 0.15)
    assert isinstance(load_session_config(Path("config/paper.yaml")), PaperConfig)
    assert LiveConfig(mode="testnet", strategies=live.strategies).ledger == Path(
        "data/testnet.sqlite"
    )
    assert LiveConfig(mode="live", strategies=live.strategies).ledger == Path("data/live.sqlite")

    monkeypatch.chdir(tmp_path)  # no .env here: only the environment counts
    for name in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "HEARTBEAT_URL", "BINANCE_API_KEY"):
        monkeypatch.delenv(f"TBOT_{name}", raising=False)
    empty = Settings()
    assert empty.telegram_token is None
    assert empty.binance_api_key is None
    monkeypatch.setenv("TBOT_TELEGRAM_TOKEN", "token")
    monkeypatch.setenv("TBOT_TELEGRAM_CHAT_ID", "42")
    configured = Settings()
    assert (configured.telegram_token, configured.telegram_chat_id) == ("token", "42")

    async def build() -> tuple[object, object]:
        async with httpx.AsyncClient() as client:
            return make_notifier(configured, client), make_notifier(empty, client)

    telegram, fallback = asyncio.run(build())
    assert isinstance(telegram, Telegram)
    assert isinstance(fallback, LogNotifier)


def test_status_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path = tmp_path / "paper.yaml"
    config_path.write_text(
        "strategies:\n"
        "  - {name: donchian_trend, symbols: [BTCUSDT], timeframe: 4h, allocation: 1.0}\n"
        f"ledger: {(tmp_path / 'paper.sqlite').as_posix()}\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="no ledger"):  # reading must not create one
        main(["status", str(config_path), "--data-dir", str(tmp_path / "data")])
    Ledger(tmp_path / "paper.sqlite").close()
    assert main(["status", str(config_path), "--data-dir", str(tmp_path / "data")]) == 0
    out = capsys.readouterr().out
    assert "no equity snapshots yet" in out
    assert "no bars BTCUSDT" in out
    assert main(["resume", str(config_path)]) == 0
    assert "not halted" in capsys.readouterr().out


def test_live_command_needs_the_flag_for_real_money(tmp_path: Path) -> None:
    config_path = tmp_path / "live.yaml"
    config_path.write_text(
        "mode: live\nstrategies:\n"
        "  - {name: donchian_trend, symbols: [BTCUSDT], timeframe: 4h, allocation: 1.0}\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="--live"):
        main(["live", str(config_path), "--data-dir", str(tmp_path / "data")])
