import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from binance_spot_fake import FakeSpot
from tbot.core.config import StrategyConfig
from tbot.core.models import Fill
from tbot.core.timeframe import Timeframe
from tbot.exchange.binance import TESTNET_URL, BinanceSpot
from tbot.live.config import PaperConfig
from tbot.live.ledger import Adjustment, Ledger
from tbot.live.reconcile import Ownership, reconcile
from tbot.live.runner import restore_portfolio
from tbot.portfolio.portfolio import Portfolio

T0 = datetime(2024, 1, 1, tzinfo=UTC)
BTC = "BTCUSDT"


class Collect:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


def run_reconcile(
    fake: FakeSpot,
    portfolio: Portfolio,
    tmp_path: Path,
    times: int = 1,
    ownership: Ownership = "account",
) -> tuple[list[list[Adjustment]], Ledger, Collect]:
    async def go() -> list[list[Adjustment]]:
        async with fake.client() as client:
            spot = BinanceSpot(client, fake.api_key, fake.secret, base_url=TESTNET_URL)
            await spot.load_rules([BTC, "ETHUSDT"])
            return [
                await reconcile(
                    spot,
                    portfolio,
                    [BTC, "ETHUSDT"],
                    ledger,
                    notifier,
                    T0,
                    tolerance=0.002,
                    ownership=ownership,
                )
                for _ in range(times)
            ]

    ledger, notifier = Ledger(tmp_path / "ledger.sqlite"), Collect()
    results = asyncio.run(go())
    return results, ledger, notifier


def test_adopts_exchange_balances(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 950.0, "BTC": 0.02}, prices={BTC: 50000.0})
    portfolio = Portfolio(1000.0)
    (first, second), ledger, notifier = run_reconcile(fake, portfolio, tmp_path, times=2)

    assert [(a.symbol, a.quantity, a.cash) for a in first] == [
        (BTC, pytest.approx(0.02), 0.0),
        ("", 0.0, pytest.approx(-50.0)),
    ]
    assert second == []
    assert portfolio.position(BTC) == pytest.approx(0.02)
    assert portfolio.cash == pytest.approx(950.0)
    assert len(ledger.adjustments()) == 2
    assert [e.level for e in ledger.recent_events(5)] == ["warning", "warning"]
    assert notifier.messages[0].startswith("[live] book adjusted to the exchange")
    ledger.close()


def test_small_differences_and_locked_balances_are_not_adjusted(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 999.5, "BTC": 0.02}, prices={BTC: 50000.0})
    fake.balances["BTC"] -= 0.01
    fake.locked["BTC"] = 0.01  # held by a stop order: still ours
    portfolio = Portfolio(1000.0)
    portfolio.positions[BTC] = 0.020004  # within one step of the exchange
    [adjustments], ledger, notifier = run_reconcile(fake, portfolio, tmp_path)
    assert adjustments == []
    assert notifier.messages == []
    ledger.close()


def test_restore_replays_fills_and_adjustments_in_order(tmp_path: Path) -> None:
    config = PaperConfig(
        initial_cash=1000.0,
        strategies=[
            StrategyConfig(
                name="donchian_trend", symbols=[BTC], timeframe=Timeframe.H4, allocation=1
            )
        ],
        ledger=tmp_path / "ledger.sqlite",
    )
    ledger = Ledger(config.ledger)
    ledger.add_fill(Fill(T0, BTC, 0.01, 50000.0, 0.5), 50000.0)
    ledger.add_adjustment(Adjustment(T0 + timedelta(hours=1), BTC, 0.02, -50.0, "exchange"))
    ledger.add_adjustment(Adjustment(T0 + timedelta(hours=2), "", 0.0, 5.0, "fee rebate"))
    ledger.close()

    with Ledger(config.ledger) as reopened:
        portfolio = restore_portfolio(config, reopened)
    assert portfolio.position(BTC) == pytest.approx(0.03)
    assert portfolio.cash == pytest.approx(1000 - 500 - 0.5 - 50 + 5)


def test_budget_ownership_leaves_the_owners_balances_alone(tmp_path: Path) -> None:
    fake = FakeSpot(balances={"USDT": 5000.0, "BTC": 0.3}, prices={BTC: 50000.0})
    portfolio = Portfolio(1000.0)
    portfolio.positions[BTC] = 0.01  # the bot's; the rest of the BTC and USDT is the owner's
    [adjustments], ledger, _ = run_reconcile(fake, portfolio, tmp_path, ownership="budget")
    assert adjustments == []
    ledger.close()

    fake.balances.update(USDT=600.0, BTC=0.004)  # the owner withdrew and sold below the book
    [adjustments], ledger, _ = run_reconcile(fake, portfolio, tmp_path, ownership="budget")
    assert [(a.symbol, a.quantity, a.cash) for a in adjustments] == [
        (BTC, pytest.approx(-0.006), 0.0),
        ("", 0.0, pytest.approx(-400.0)),
    ]
    assert portfolio.position(BTC) == pytest.approx(0.004)
    assert portfolio.cash == pytest.approx(600.0)
    ledger.close()
