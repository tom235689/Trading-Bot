"""Messages the bot sends: the daily summary, fills, and stale streams."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from factories import price_bars
from tbot.core.config import StrategyConfig
from tbot.core.models import Fill
from tbot.core.text import price_text
from tbot.core.timeframe import Timeframe
from tbot.data.store import BarStore
from tbot.live import runner
from tbot.live.config import PaperConfig
from tbot.live.history import BarHistory
from tbot.live.ledger import Adjustment, EquityPoint, Ledger
from tbot.live.overview import session_row
from tbot.live.runner import _report_stale, fills_text, summary_text
from tbot.live.session import TradingSession
from tbot.portfolio.allocation import build_slots
from tbot.portfolio.portfolio import Portfolio

H1, H4 = Timeframe.H1, Timeframe.H4
T0 = datetime(2024, 1, 1, tzinfo=UTC)
KEY = ("BTCUSDT", H1)


class Collect:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


def test_the_summary_values_coins_moved_out_at_their_time(tmp_path: Path) -> None:
    # 0.5 BTC leaves the book at 5000; a day later BTC is 6000. Trading made +5%.
    config = PaperConfig(
        initial_cash=10_000.0,
        strategies=[
            StrategyConfig(name="donchian_trend", symbols=["BTCUSDT"], timeframe=H1, allocation=1.0)
        ],
        ledger=tmp_path / "paper.sqlite",
    )
    store = BarStore(tmp_path / "data")
    closes = [5000.0] * 2 + [6000.0] * 24
    store.write(*KEY, price_bars(T0, H1, closes, closes))
    now = T0 + timedelta(hours=26)
    with Ledger(config.ledger) as ledger:
        ledger.set_meta("base_cash", "10000.0")
        ledger.add_equity(EquityPoint(T0 + timedelta(hours=1), 10_000.0, 5_000.0, 0.5))
        ledger.add_adjustment(Adjustment(T0 + timedelta(hours=2), "BTCUSDT", -0.5, 0.0, "owner"))
        ledger.add_equity(EquityPoint(now, 8_000.0, 5_000.0, 0.375))
        histories = {KEY: BarHistory(H1, store.read(*KEY))}
        session = TradingSession(
            config, build_slots(config.strategies), histories, Portfolio(5_000.0)
        )
        session.portfolio.positions["BTCUSDT"] = 0.5
        text = summary_text(session, ledger, now, "paper", store=store)
    assert "+5.00% over 24h" in text
    assert session_row("paper", config, store).day_change == pytest.approx(0.05)  # agrees


def test_one_summary_a_day_when_the_wall_clock_steps_back(monkeypatch: pytest.MonkeyPatch) -> None:
    wall = [datetime(2024, 1, 1, 23, 0, tzinfo=UTC)]
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 4:
            raise asyncio.CancelledError
        step_back = timedelta(milliseconds=10) if len(sleeps) == 1 else timedelta(0)
        wall[0] += timedelta(seconds=seconds) - step_back  # the sleep is monotonic

    monkeypatch.setattr(runner, "utc_now", lambda: wall[0])
    monkeypatch.setattr(runner, "asyncio", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(runner, "summary_text", lambda *args, **kwargs: f"{args[2]:%Y-%m-%d}")
    notifier = Collect()

    async def run() -> None:
        with pytest.raises(asyncio.CancelledError):
            await runner.summary_loop(None, None, notifier, 0, "paper")  # type: ignore[arg-type]

    asyncio.run(run())
    assert notifier.messages == ["2024-01-02", "2024-01-03"]


def test_prices_below_a_cent_keep_their_digits() -> None:
    assert price_text(0.00001234) == "0.00001234"
    assert price_text(0.5) == "0.50"
    assert price_text(0.123456789) == "0.123457"
    assert price_text(64_250.5) == "64,250.50"

    class Fills:
        def recent_fills(self, count: int) -> list[Fill]:
            return [Fill(T0, "PEPEUSDT", 2_000_000.0, 0.00001234, 0.02)]

    assert "PEPEUSDT @ 0.00001234" in fills_text(Fills(), "paper")  # type: ignore[arg-type]


def test_a_stale_stream_alert_names_the_close(tmp_path: Path) -> None:
    notifier = Collect()

    async def run() -> None:
        with Ledger(tmp_path / "l.sqlite") as ledger:
            # The 08:00 bar of a 4h stream closed at 12:00.
            _report_stale(ledger, notifier, "paper", ("BTCUSDT", H4), T0.replace(hour=8))
            await asyncio.sleep(0.05)

    asyncio.run(run())
    assert notifier.messages == [
        "[paper] stale stream BTCUSDT 4h: last bar closed 2024-01-01 12:00 UTC"
    ]
