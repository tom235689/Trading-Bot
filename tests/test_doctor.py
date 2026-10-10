import asyncio
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from binance_spot_fake import FakeSpot
from factories import price_bars
from tbot.core.config import StrategyConfig
from tbot.core.timeframe import Timeframe
from tbot.data.store import BarStore
from tbot.live.backup import backup_ledger, daily_backups
from tbot.live.config import LiveConfig, PaperConfig, Settings
from tbot.live.doctor import Check, backup_check, checks_text, guard_check, run_checks
from tbot.live.ledger import Ledger
from tbot.live.runner import GUARD_META, KEYS_MISSING, MODE_META, stop_path
from tbot.risk.guard import GuardConfig, GuardState

STRATEGIES = [
    StrategyConfig(
        name="donchian_trend",
        symbols=["BTCUSDT", "ETHUSDT"],
        timeframe=Timeframe.H4,
        allocation=1.0,
        params={"entry": 55, "exit": 20},
    )
]
T0 = datetime(2024, 1, 1, tzinfo=UTC)
ALERTS = {"telegram_token": "t", "telegram_chat_id": "1", "heartbeat_url": "https://hc/ping"}
KEYS = {"binance_api_key": "key", "binance_api_secret": "secret"}


def settings(**values: str) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def live(tmp_path: Path, mode: str = "live", **update: object) -> LiveConfig:
    return LiveConfig.model_validate(
        {
            "mode": mode,
            "initial_cash": 1000,
            "strategies": STRATEGIES,
            "ledger": tmp_path / f"{mode}.sqlite",
            **update,
        }
    )


def fake(**update: object) -> FakeSpot:
    spot = FakeSpot(balances={"USDT": 5000.0}, prices={"BTCUSDT": 60000.0, "ETHUSDT": 3000.0})
    for name, value in update.items():
        setattr(spot, name, value)
    return spot


def check(config: LiveConfig | PaperConfig, values: Settings, spot: FakeSpot) -> list[Check]:
    async def run() -> list[Check]:
        async with spot.client() as client:
            return await run_checks(config, values, client)

    return asyncio.run(run())


def by_name(checks: list[Check]) -> dict[str, list[tuple[str, str]]]:
    found: dict[str, list[tuple[str, str]]] = {}
    for c in checks:
        found.setdefault(c.name, []).append((c.status, c.detail))
    return found


def test_a_ready_live_setup_passes(tmp_path: Path) -> None:
    checks = check(live(tmp_path), settings(**ALERTS, **KEYS), fake())
    assert [c for c in checks if c.status != "ok"] == []
    names = [c.name for c in checks]
    assert names == [
        "strategies",
        "ledger",
        "telegram",
        "heartbeat",
        "clock",
        "symbols",
        "api key",
        "permissions",
        "budget",
        "mode",
    ]
    assert checks_text(checks).endswith("0 problems, 0 warnings")


def test_dangerous_key_and_short_budget_fail(tmp_path: Path) -> None:
    rights = {
        "ipRestrict": False,
        "enableWithdrawals": True,
        "enableSpotAndMarginTrading": True,
    }
    spot = fake(restrictions=rights, balances={"USDT": 400.0})
    found = by_name(check(live(tmp_path), settings(**ALERTS, **KEYS), spot))
    assert ("fail", "the key can withdraw; turn that off") in found["permissions"]
    assert found["permissions"][-1][0] == "warn"  # any IP
    assert found["budget"][0][0] == "fail"
    assert "USDT 400.00 free, the bot needs 1,000.00" in found["budget"][0][1]


def test_testnet_skips_permissions_and_flags_a_wrong_key(tmp_path: Path) -> None:
    config = live(tmp_path, mode="testnet")
    found = by_name(check(config, settings(**ALERTS, **KEYS), fake()))
    assert "permissions" not in found
    assert "mode" not in found
    wrong = by_name(
        check(config, settings(**ALERTS, binance_api_key="x", binance_api_secret="y"), fake())
    )
    assert wrong["api key"][0][0] == "fail"
    assert "-2015" in wrong["api key"][0][1]
    missing = by_name(check(config, settings(**ALERTS), fake()))
    assert missing["api key"] == [("fail", KEYS_MISSING)]


def test_paper_ledger_alerts_and_network(tmp_path: Path) -> None:
    config = PaperConfig(strategies=STRATEGIES, ledger=tmp_path / "paper.sqlite")
    with Ledger(config.ledger) as ledger:
        halted = GuardState(halted=True, halt_reason="drawdown 16% beyond 15%")
        ledger.set_meta(GUARD_META, halted.model_dump_json())
    found = by_name(check(config, settings(telegram_token="t"), fake()))
    assert found["kill switch"] == [("fail", "halted: drawdown 16% beyond 15%; run `tbot resume`")]
    assert found["telegram"][0][0] == "fail"
    assert found["heartbeat"][0][0] == "warn"
    assert found["symbols"] == [("ok", "BTCUSDT, ETHUSDT trading")]

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    async def offline() -> list[Check]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
            return await run_checks(config, settings(), client)

    assert by_name(asyncio.run(offline()))["binance"][0][0] == "fail"


def test_a_config_that_cannot_start_fails_first(tmp_path: Path) -> None:
    typo = STRATEGIES[0].model_copy(update={"name": "donchian_trnd"})
    config = PaperConfig(strategies=[typo], ledger=tmp_path / "paper.sqlite")
    checks = check(config, settings(**ALERTS), fake())
    assert checks[0].status == "fail"
    assert "unknown strategy 'donchian_trnd'" in checks[0].detail
    wrong = STRATEGIES[0].model_copy(update={"params": {"entry": 0}})
    bad = by_name(check(PaperConfig(strategies=[wrong], ledger=config.ledger), settings(), fake()))
    assert bad["strategies"][0][0] == "fail"


def test_ledger_and_heartbeat_problems(tmp_path: Path) -> None:
    folder = tmp_path / "ledger.sqlite"
    folder.mkdir()
    found = by_name(
        check(
            PaperConfig(strategies=STRATEGIES, ledger=folder),
            settings(heartbeat_url="hc-ping.com/abc"),
            fake(),
        )
    )
    assert found["ledger"][0][0] == "fail"
    assert found["heartbeat"] == [("fail", "TBOT_HEARTBEAT_URL must be a full http(s):// URL")]
    broken = tmp_path / "broken.sqlite"
    broken.write_text("not a database", encoding="utf-8")
    found = by_name(check(PaperConfig(strategies=STRATEGIES, ledger=broken), settings(), fake()))
    assert found["ledger"][0][0] == "fail"
    assert "not a usable tbot ledger" in found["ledger"][0][1]


def test_kill_switch_is_tried_on_the_stored_history(tmp_path: Path) -> None:
    store = BarStore(tmp_path / "data")
    closes = [100.0] * 2600  # over a year after the 90-day warmup
    closes += [closes[-1] * 1.02**i for i in range(1, 21)]  # a breakout
    closes += [closes[-1] * 0.99**i for i in range(1, 200)]  # then a long slide
    short = BarStore(tmp_path / "short")
    for symbol in ("BTCUSDT", "ETHUSDT"):
        opens = [100.0, *closes[:-1]]
        store.write(symbol, Timeframe.H4, price_bars(T0, Timeframe.H4, opens, closes))
        short.write(symbol, Timeframe.H4, price_bars(T0, Timeframe.H4, opens[:900], closes[:900]))
    holds = [STRATEGIES[0].model_copy(update={"params": {"entry": 5, "exit": 600}})]
    tight = PaperConfig(
        strategies=holds, ledger=tmp_path / "p.sqlite", guard=GuardConfig(max_drawdown=0.1)
    )
    result = guard_check(tight, store)
    assert result.status == "warn"
    assert "would have halted" in result.detail
    loose = tight.model_copy(update={"guard": GuardConfig(max_drawdown=0.9)})
    assert guard_check(loose, store).status == "ok"
    assert "run `tbot download`" in guard_check(tight, BarStore(tmp_path / "none")).detail
    few = guard_check(tight, short)  # what a paper start leaves: no verdict either way
    assert (few.status, few.detail[:42]) == ("warn", "too little stored history to test it (59 d")


def test_ledger_backups_are_checked(tmp_path: Path) -> None:
    config = PaperConfig(strategies=STRATEGIES, ledger=tmp_path / "paper.sqlite")
    with Ledger(config.ledger) as ledger:
        ledger.add_event(T0, "info", "started")
    assert backup_check(config).detail.startswith("none yet")
    with Ledger(config.ledger) as ledger:
        backup_ledger(ledger, config.ledger, T0, keep=14)
    assert backup_check(config).status == "ok"
    old = time.time() - 5 * 86400
    os.utime(daily_backups(config.ledger)[-1], (old, old))
    assert "5 days older" in backup_check(config).detail
    assert backup_check(config.model_copy(update={"backup_days": 0})).detail.startswith("off")


def test_offline_doctor_skips_what_the_bot_retries(tmp_path: Path) -> None:
    async def run(found: Settings) -> list[Check]:
        async with fake().client() as client:
            return await run_checks(live(tmp_path), found, client, offline=True)

    names = set(by_name(asyncio.run(run(settings(**ALERTS, **KEYS)))))
    assert {"strategies", "ledger", "telegram"} <= names
    assert not names & {"heartbeat", "binance", "clock", "symbols", "api key"}  # no network
    # Keys that are not set at all fail every start: no network needed to say so.
    missing = by_name(asyncio.run(run(settings(**ALERTS))))
    assert missing["api key"] == [("fail", KEYS_MISSING)]


def test_a_ledger_of_another_mode_and_a_waiting_stop_are_named(tmp_path: Path) -> None:
    config = PaperConfig(strategies=STRATEGIES, ledger=tmp_path / "live.sqlite")
    with Ledger(config.ledger) as ledger:
        ledger.set_meta(MODE_META, "live")
    stop_path(config.ledger).write_text("stop\n", encoding="utf-8")
    found = by_name(check(config, settings(**ALERTS), fake()))
    assert found["ledger"][0][0] == "fail"
    assert "keeps a live book, not a paper one" in found["ledger"][0][1]
    assert found["stop"][0][0] == "warn"
