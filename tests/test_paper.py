import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from factories import Scripted, price_bars
from tbot.cli import main
from tbot.core.config import StrategyConfig
from tbot.core.timeframe import Timeframe
from tbot.data.store import BarStore
from tbot.execution.rebalance import RebalanceRules
from tbot.execution.sim_broker import CostModel, SimulatedBroker
from tbot.live.config import PaperConfig, Settings, load_paper_config
from tbot.live.history import BarHistory
from tbot.live.ledger import Ledger
from tbot.live.paper import (
    PaperTrader,
    PriceSource,
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

H1, H4 = Timeframe.H1, Timeframe.H4
T0 = datetime(2024, 1, 1, tzinfo=UTC)
BTC = ("BTC", H1)


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
    )


def make_trader(
    tmp_path: Path, script: dict[datetime, dict[str, float]], prices: PriceSource
) -> tuple[PaperTrader, Ledger, Collect]:
    config = paper_config(tmp_path)
    session = TradingSession(
        config,
        [StrategySlot(Scripted(["BTC"], {"script": script}), H1, 1.0)],
        {BTC: BarHistory(H1)},
        Portfolio(config.initial_cash),
    )
    ledger = Ledger(config.ledger)
    notifier = Collect()
    trader = PaperTrader(
        session, ledger, SimulatedBroker(config.costs), prices, notifier, clock=lambda: at(9)
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
    assert ledger.counts() == {"fills": 2, "orders": 2, "signals": 3, "equity": 3, "events": 0}
    latest = ledger.latest_equity()
    assert latest is not None
    assert latest.equity == pytest.approx(1000 - 0.5 - 0.5)  # two fees, flat price
    assert latest.time == at(3)
    assert notifier.messages[0].startswith("[paper] BUY 5.000000 BTC @ 100.00")
    assert notifier.messages[1].startswith("[paper] SELL 5.000000 BTC @ 100.00")
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
    summary = summary_text(trader.session, ledger, at(1))
    assert "equity 999.50" in summary
    assert "BTC 5.000000" in summary
    ledger.close()

    store = BarStore(tmp_path / "data")
    store.write("BTC", H1, BARS)
    status = status_text(paper_config(tmp_path), store)
    assert "fills 1, round trips 0" in status
    assert "position BTC 5.000000" in status
    assert "last stored bar BTC: 2024-01-01 02:00" in status


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
    assert lookback_bars(build_slots(config.strategies)) == {
        ("BTCUSDT", H4): 12,
        ("ETHUSDT", H1): 22,
    }


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


def test_paper_config_and_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_paper_config(Path("config/paper.yaml"))
    assert config.ledger == Path("data/paper.sqlite")
    assert config.risk.max_symbol_weight == 0.5

    monkeypatch.chdir(tmp_path)  # no .env here: only the environment counts
    for name in ("TBOT_TELEGRAM_TOKEN", "TBOT_TELEGRAM_CHAT_ID", "TBOT_HEARTBEAT_URL"):
        monkeypatch.delenv(name, raising=False)
    empty = Settings()
    assert empty.telegram_token is None
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
    assert main(["status", str(config_path), "--data-dir", str(tmp_path / "data")]) == 0
    out = capsys.readouterr().out
    assert "no equity snapshots yet" in out
    assert "no bars BTCUSDT" in out
