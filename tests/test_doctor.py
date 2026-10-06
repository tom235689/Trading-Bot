import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx

from binance_spot_fake import FakeSpot
from factories import price_bars
from tbot.core.config import StrategyConfig
from tbot.core.timeframe import Timeframe
from tbot.data.store import BarStore
from tbot.live.config import LiveConfig, PaperConfig, Settings
from tbot.live.doctor import Check, checks_text, guard_check, run_checks
from tbot.live.ledger import Ledger
from tbot.live.runner import GUARD_META
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
    assert missing["api key"] == [("fail", "set TBOT_BINANCE_API_KEY and TBOT_BINANCE_API_SECRET")]


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
    assert found["heartbeat"] == [("fail", "TBOT_HEARTBEAT_URL must start with https://")]
    broken = tmp_path / "broken.sqlite"
    broken.write_text("not a database", encoding="utf-8")
    found = by_name(check(PaperConfig(strategies=STRATEGIES, ledger=broken), settings(), fake()))
    assert found["ledger"][0][0] == "fail"
    assert "not a usable tbot ledger" in found["ledger"][0][1]


def test_kill_switch_is_tried_on_the_stored_history(tmp_path: Path) -> None:
    store = BarStore(tmp_path / "data")
    closes = [100.0] * 700
    closes += [closes[-1] * 1.02**i for i in range(1, 21)]  # a breakout
    closes += [closes[-1] * 0.99**i for i in range(1, 200)]  # then a long slide
    for symbol in ("BTCUSDT", "ETHUSDT"):
        opens = [100.0, *closes[:-1]]
        store.write(symbol, Timeframe.H4, price_bars(T0, Timeframe.H4, opens, closes))
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
