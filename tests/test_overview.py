"""`tbot status` without a config: one row per session."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tbot.core.config import StrategyConfig
from tbot.core.models import Fill
from tbot.core.timeframe import Timeframe
from tbot.data.store import BarStore
from tbot.live.config import PaperConfig
from tbot.live.ledger import Adjustment, EquityPoint, Ledger
from tbot.live.overview import SessionRow, overview_text, session_row
from tbot.live.runner import BASE_META, GUARD_META, instance_lock
from tbot.risk.guard import GuardState

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def paper(tmp_path: Path) -> PaperConfig:
    strategy = StrategyConfig(
        name="donchian_trend", symbols=["BTCUSDT"], timeframe=Timeframe.H4, allocation=1.0
    )
    return PaperConfig(initial_cash=1000.0, strategies=[strategy], ledger=tmp_path / "p.sqlite")


def test_a_row_counts_money_put_in_apart_from_profit(tmp_path: Path) -> None:
    config = paper(tmp_path)
    store = BarStore(tmp_path / "data")
    with Ledger(config.ledger) as ledger:
        ledger.set_meta(BASE_META, "1000")
        ledger.add_fill(Fill(T0, "BTCUSDT", 0.01, 50_000.0, 0.5), reference_price=50_000.0)
        ledger.add_equity(EquityPoint(T0, 1000.0, 499.5, 0.5))
        ledger.add_adjustment(Adjustment(T0 + timedelta(hours=1), "", 0.0, 500.0, "budget"))
        ledger.add_equity(EquityPoint(T0 + timedelta(days=1), 1600.0, 999.5, 0.4))
        later = Adjustment(T0 + timedelta(days=2), "", 0.0, 300.0, "budget")  # not in it yet
        ledger.add_adjustment(later)
        halted = GuardState(peak_equity=1700.0, halted=True, halt_reason="drawdown 46%")
        ledger.set_meta(GUARD_META, halted.model_dump_json())
        ledger.add_order(T0, "BTCUSDT", 0.01, "pending", client_id="x1")
    row = session_row("paper", config, store)
    assert row.state == "stopped"
    assert row.equity == 1600.0
    assert row.profit == pytest.approx(100.0)  # 1000 to start and 500 added later
    assert row.day_change == pytest.approx(0.6)
    assert row.from_peak == pytest.approx(1600.0 / 1700.0 - 1)
    assert row.symbols == ("BTCUSDT",)
    assert row.notes == (
        "HALTED: drawdown 46% (tbot resume paper)",
        "1 order(s) in doubt, settled when it runs",
    )
    with instance_lock(config.ledger):
        assert session_row("paper", config, store).state == "running"

    text = overview_text([row, SessionRow("testnet", "not started")])
    lines = text.splitlines()
    assert lines[0].split() == [
        "config", "state", "equity", "profit", "24h", "peak", "last", "bar", "(UTC)", "positions"
    ]  # fmt: skip
    assert lines[1].split() == [
        "paper", "stopped", "1,600.00", "+100.00", "(+6.7%)", "+60.0%", "-5.9%",
        "2026-10-02", "00:00", "BTCUSDT",
    ]  # fmt: skip
    assert lines[2].strip() == "HALTED: drawdown 46% (tbot resume paper)"
    assert lines[4].split() == ["testnet", "not", "started"]
    assert lines[-1].startswith("details: tbot status <config>")


def test_rows_without_a_ledger_or_with_a_broken_one(tmp_path: Path) -> None:
    config = paper(tmp_path)
    store = BarStore(tmp_path / "data")
    assert session_row("paper", config, store) == SessionRow("paper", "not started")
    assert not config.ledger.exists()  # looking never creates a ledger
    text = overview_text([SessionRow("paper", "not started")])
    assert text.splitlines() == [
        "config   state",
        "paper    not started",
        "nothing has run yet: `tbot doctor` checks the setup, `tbot paper` starts",
    ]
    config.ledger.write_text("not a database", encoding="utf-8")
    row = session_row("paper", config, store)
    assert row.state == "stopped"
    assert row.notes[0].startswith("ledger unusable:")
    with Ledger(tmp_path / "new.sqlite"):
        pass
    empty = session_row(
        "new", paper(tmp_path).model_copy(update={"ledger": tmp_path / "new.sqlite"}), store
    )
    assert empty.notes == ("no bar event yet",)
