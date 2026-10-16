import asyncio
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from binance_spot_fake import FakeSpot
from factories import price_bars
from tbot.core.config import StrategyConfig
from tbot.core.models import Fill
from tbot.core.timeframe import Timeframe
from tbot.data.store import BarStore
from tbot.exchange.binance import BinanceError, BinanceSpot
from tbot.live.backup import backup_ledger, daily_backups
from tbot.live.config import ONE_QUOTE, LiveConfig, PaperConfig, Settings
from tbot.live.doctor import (
    Check,
    backup_check,
    checks_text,
    guard_check,
    run_checks,
    synced_folder,
    works_on_testnet,
)
from tbot.live.ledger import Adjustment, Ledger
from tbot.live.runner import (
    BUDGET_META,
    GUARD_META,
    KEYS_MISSING,
    MODE_META,
    halt_path,
    key_hint,
    mode_problem,
    other_books_cash,
    stop_path,
)
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


def test_the_budget_check_counts_free_cash_other_books_keep_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tbot.live.doctor.other_books_cash", other_books_cash)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "eth.yaml").write_text(
        "mode: live\ninitial_cash: 1000\nledger: eth.sqlite\nstrategies:\n"
        "  - {name: donchian_trend, symbols: [ETHUSDT], timeframe: 4h, allocation: 1}\n",
        "utf-8",
    )
    with Ledger(tmp_path / "eth.sqlite") as book:
        book.set_meta(BUDGET_META, repr(1000.0))
    spot = fake(balances={"USDT": 1500.0})
    found = by_name(check(live(tmp_path), settings(**ALERTS, **KEYS), spot))
    [(status, detail)] = found["budget"]
    assert status == "fail"  # as the start refuses it
    assert detail.startswith(
        "USDT 1,500.00 free (1,000.00 of it the cash of eth), the bot needs 1,000.00"
    )


def test_symbols_quoted_in_two_assets_fail(tmp_path: Path) -> None:
    spot = fake()
    spot.rules = {**spot.rules, "ETHBUSD": spot.rules["ETHUSDT"]}
    mixed = StrategyConfig(
        name="donchian_trend",
        symbols=["BTCUSDT", "ETHBUSD"],
        timeframe=Timeframe.H4,
        allocation=1.0,
    )
    found = by_name(check(live(tmp_path, strategies=[mixed]), settings(**ALERTS, **KEYS), spot))
    assert found["symbols"] == [("fail", f"quoted in BUSD, USDT: {ONE_QUOTE}")]


def test_a_rejected_key_names_the_pair_the_session_uses() -> None:
    error = BinanceError(-2014, "API-key format invalid.")
    both = settings(**KEYS, binance_testnet_api_key="k", binance_testnet_api_secret="s")
    assert "check TBOT_BINANCE_TESTNET_API_KEY and" in key_hint(error, both.key_names("testnet"))
    assert "check TBOT_BINANCE_API_KEY and" in key_hint(error, both.key_names("live"))
    assert "check TBOT_BINANCE_API_KEY and" in key_hint(
        error, settings(**KEYS).key_names("testnet")
    )


def test_whether_a_key_works_on_the_testnet_can_be_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run(key: str) -> bool | None:
        async with fake().client() as client:
            return await works_on_testnet(key, "secret", client)

    assert asyncio.run(run("key")) is True
    assert asyncio.run(run("other")) is False

    async def unreachable(public: BinanceSpot) -> float:
        raise httpx.ConnectError("no network")

    monkeypatch.setattr("tbot.live.doctor.server_offset", unreachable)
    assert asyncio.run(run("key")) is None  # an outage says nothing about the key


def test_rights_the_bot_never_uses_are_warned_about(tmp_path: Path) -> None:
    spot = fake()
    spot.restrictions = {**spot.restrictions, "enableFutures": True}
    found = by_name(check(live(tmp_path), settings(**ALERTS, **KEYS), spot))
    assert found["permissions"] == [
        ("warn", "the key also allows futures trading: tbot never uses it")
    ]


def test_account_ownership_names_what_the_bot_takes_over(tmp_path: Path) -> None:
    spot = fake(balances={"USDT": 5000.0, "BTC": 0.25, "DOGE": 900.0})
    found = by_name(check(live(tmp_path, ownership="account"), settings(**ALERTS, **KEYS), spot))
    [(status, detail)] = found["budget"]
    assert status == "warn"
    assert detail.startswith("ownership account: the bot takes over and trades all of ")
    assert "USDT 5,000" in detail
    assert "BTC 0.25" in detail
    assert "DOGE" not in detail  # a pair it does not trade stays out of it


def test_a_halt_request_no_session_took_is_named(tmp_path: Path) -> None:
    config = PaperConfig(strategies=STRATEGIES, ledger=tmp_path / "paper.sqlite")
    Ledger(config.ledger).close()
    halt_path(config.ledger).write_text("halt\n", encoding="utf-8")
    found = by_name(check(config, settings(), fake()))
    assert found["halt"] == [
        (
            "warn",
            "a `tbot halt` request waits: the next start sells what the bot holds and halts "
            "(`tbot resume` withdraws it)",
        )
    ]


def test_a_synced_folder_is_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        monkeypatch.delenv(name, raising=False)
    assert synced_folder(tmp_path / "Projects" / "Trading-Bot") == ""
    assert synced_folder(tmp_path / "Dropbox (Personal)" / "Trading-Bot") == "Dropbox"
    assert synced_folder(tmp_path / "OneDrive - Contoso" / "Trading-Bot") == "OneDrive"
    monkeypatch.setenv("OneDrive", str(tmp_path / "Cloud"))  # a renamed OneDrive folder
    assert synced_folder(tmp_path / "Cloud" / "bots" / "Trading-Bot") == "OneDrive"
    assert synced_folder(tmp_path / "CloudArchive" / "Trading-Bot") == ""  # beside it


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

    unreachable = by_name(asyncio.run(offline()))["binance"][0]
    assert unreachable[0] == "fail"
    assert "Binance may not serve this region" in unreachable[1]


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
    hint = "run `tbot download --symbols BTCUSDT ETHUSDT --timeframes 4h"
    assert hint in guard_check(tight, BarStore(tmp_path / "none")).detail
    few = guard_check(tight, short)  # what a paper start leaves: no verdict either way
    assert (few.status, few.detail[:42]) == ("warn", "too little stored history to test it (59 d")


def test_a_turnover_limit_the_strategy_trips_on_history_is_named(tmp_path: Path) -> None:
    store = BarStore(tmp_path / "data")
    closes = [100.0] * 2600  # over a year after the 90-day warmup
    closes += [closes[-1] * 1.02**i for i in range(1, 21)]  # a breakout
    closes += [closes[-1]] * 200
    for symbol, lag in (("BTCUSDT", 0), ("ETHUSDT", 3)):  # ETH breaks out three bars later
        shifted = [100.0] * lag + closes[: len(closes) - lag]
        opens = [100.0, *shifted[:-1]]
        store.write(symbol, Timeframe.H4, price_bars(T0, Timeframe.H4, opens, shifted))
    holds = [STRATEGIES[0].model_copy(update={"params": {"entry": 5, "exit": 600}})]
    guard = GuardConfig(max_drawdown=0.9, max_daily_turnover=0.1)  # BTC's buy alone is more
    config = PaperConfig(strategies=holds, ledger=tmp_path / "p.sqlite", guard=guard)
    result = guard_check(config, store)
    assert result.status == "warn"
    assert result.detail.startswith("90% never tripped, but the 0.1x turnover limit would have ")
    assert "raise guard.max_daily_turnover above what the strategy trades" in result.detail
    loose = config.model_copy(update={"guard": GuardConfig(max_drawdown=0.9)})
    assert guard_check(loose, store).status == "ok"


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


def test_real_money_needs_alerts_and_a_failed_ping_keeps_its_url_secret(tmp_path: Path) -> None:
    found = by_name(check(live(tmp_path), settings(**KEYS), fake()))
    assert found["telegram"][0][0] == "fail"  # nobody would hear that it failed
    testnet = by_name(check(live(tmp_path, mode="testnet"), settings(**KEYS), fake()))
    assert testnet["telegram"][0][0] == "warn"

    secret = "https://hc-ping.com/" + "0f3e9a1c-" * 4

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    async def ping() -> list[Check]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
            config = PaperConfig(strategies=STRATEGIES, ledger=tmp_path / "paper.sqlite")
            return await run_checks(config, settings(heartbeat_url=secret), client)

    [beat] = [c for c in asyncio.run(ping()) if c.name == "heartbeat"]
    assert beat.status == "fail"
    assert "hc-ping" not in beat.detail


def test_paused_and_unknown_symbols_are_named(tmp_path: Path) -> None:
    config = PaperConfig(strategies=STRATEGIES, ledger=tmp_path / "paper.sqlite")
    found = by_name(check(config, settings(**ALERTS), fake(statuses={"ETHUSDT": "BREAK"})))
    assert found["symbols"] == [
        ("warn", "Binance does not trade ETHUSDT BREAK now; its orders wait until it does")
    ]
    gone = fake(rules={"BTCUSDT": fake().rules["BTCUSDT"]})
    assert by_name(check(config, settings(**ALERTS), gone))["symbols"] == [
        ("fail", "Binance does not list ETHUSDT")
    ]


def test_doctor_names_its_version(tmp_path: Path) -> None:
    from tbot import __version__

    config = PaperConfig(strategies=STRATEGIES, ledger=tmp_path / "paper.sqlite")
    text = checks_text(check(config, settings(**ALERTS), fake()))
    assert text.splitlines()[0] == f"tbot {__version__}"


def test_a_symbol_binance_does_not_list_is_named_among_those_asked(tmp_path: Path) -> None:
    config = PaperConfig(strategies=STRATEGIES, ledger=tmp_path / "paper.sqlite")
    spot = fake(rules={"BTCUSDT": fake().rules["BTCUSDT"]})  # no ETHUSDT
    answers = [
        httpx.Response(200),  # the heartbeat ping
        httpx.Response(200, json={"serverTime": int(time.time() * 1000)}),
        httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."}),
    ]
    spot.fail_next = answers
    assert by_name(check(config, settings(**ALERTS), spot))["symbols"] == [
        ("fail", "Binance does not list ETHUSDT")  # asked one at a time: Binance never says
    ]


def test_offline_doctor_fails_only_on_what_stops_a_start(tmp_path: Path) -> None:
    config = live(tmp_path)  # real money, no Telegram, a halted kill switch
    with Ledger(config.ledger) as ledger:
        halted = GuardState(halted=True, halt_reason="drawdown")
        ledger.set_meta(GUARD_META, halted.model_dump_json())

    async def offline(values: Settings) -> list[Check]:
        async with httpx.AsyncClient() as client:  # not used offline
            return await run_checks(config, values, client, offline=True)

    found = by_name(asyncio.run(offline(settings(**KEYS))))
    assert found["telegram"][0][0] == "fail"  # `tbot live` refuses real money without it
    assert found["kill switch"][0][0] == "warn"  # a halted bot runs, halted
    testnet = live(tmp_path, mode="testnet")

    async def offline_testnet(values: Settings) -> list[Check]:
        async with httpx.AsyncClient() as client:
            return await run_checks(testnet, values, client, offline=True)

    found = by_name(asyncio.run(offline_testnet(settings(**KEYS, telegram_token="t"))))
    assert found["telegram"][0][0] == "warn"  # `tbot update` restarts a bot that ran so
    no_keys = by_name(asyncio.run(offline(settings())))
    assert no_keys["api key"][0][0] == "fail"  # the start would fail


def test_doctor_asks_telegram_about_the_token_and_the_chat(tmp_path: Path) -> None:
    config = live(tmp_path)

    def telegram(spot: FakeSpot) -> list[tuple[str, str]]:
        return by_name(check(config, settings(**ALERTS, **KEYS), spot))["telegram"]

    assert telegram(fake()) == [
        ("ok", "Telegram knows the bot and the chat; `tbot notify` tests it")
    ]
    assert telegram(fake(telegram_token="revoked")) == [
        ("fail", "Telegram does not know TBOT_TELEGRAM_TOKEN (`tbot notify`)")
    ]
    [(status, detail)] = telegram(fake(telegram_chat="2"))
    assert (status, detail.split(":")[0]) == ("fail", "the bot cannot reach TBOT_TELEGRAM_CHAT_ID")


def test_a_ledger_of_another_mode_gets_a_free_name_suggested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = PaperConfig(strategies=STRATEGIES, ledger=Path("data/paper.sqlite"))
    with Ledger(tmp_path / "other.sqlite") as ledger:
        ledger.set_meta(MODE_META, "testnet")
        assert "(`ledger: data/paper-2.sqlite` or another new file)" in mode_problem(config, ledger)


def test_the_clock_line_says_which_way_this_computer_is_off(tmp_path: Path) -> None:
    config = PaperConfig(strategies=STRATEGIES, ledger=tmp_path / "paper.sqlite")
    [(_, detail)] = by_name(check(config, settings(**ALERTS), fake()))["clock"]
    assert detail.startswith("this computer is ")
    assert detail.endswith(("behind Binance", "ahead of Binance"))


def test_a_file_other_accounts_can_read_is_named(tmp_path: Path) -> None:
    from tbot.live.config import env_readers, save_settings

    env = tmp_path / ".env"
    save_settings(env, {"TBOT_TELEGRAM_TOKEN": "t"})  # a new .env: its owner's only
    assert env_readers(env) == []
    if sys.platform == "win32":
        subprocess.run(
            ["icacls", str(env), "/grant", "*S-1-5-32-545:R"], check=True, capture_output=True
        )
        expected = ["Users"]
    else:
        env.chmod(0o644)
        expected = ["other accounts"]
    assert env_readers(env) == expected
    save_settings(env, {"TBOT_TELEGRAM_CHAT_ID": "1"})  # replaced: its rights stay as they were
    assert env_readers(env) == expected
    assert "TBOT_TELEGRAM_CHAT_ID=1" in env.read_text(encoding="utf-8")


def test_minute_bars_in_a_session_are_warned_about(tmp_path: Path) -> None:
    fast = [s.model_copy(update={"timeframe": Timeframe.M5}) for s in STRATEGIES]
    config = PaperConfig(strategies=fast, ledger=tmp_path / "paper.sqlite")
    found = by_name(check(config, settings(**ALERTS), fake()))
    assert found["timeframe"][0][0] == "warn"


def test_a_key_binance_refuses_gets_what_to_check(tmp_path: Path) -> None:
    found = by_name(check(live(tmp_path), settings(**ALERTS, **KEYS), fake(api_key="other")))
    [(status, detail)] = found["api key"]
    assert status == "fail"
    assert "-2015" in detail
    assert "made for the other network" in detail


def test_telegram_down_or_moved_is_said_plainly() -> None:
    from tbot.live.doctor import telegram_check

    def answer(status: int, body: dict[str, object]) -> list[Check]:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/getMe"):
                return httpx.Response(200, json={"ok": True, "result": {"username": "b"}})
            return httpx.Response(status, json=body)

        async def run() -> list[Check]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                return [await telegram_check("t", "-4001", client)]

        return asyncio.run(run())

    moved = {"ok": False, "parameters": {"migrate_to_chat_id": -1009876}}
    [check_moved] = answer(400, moved)
    assert check_moved.detail.endswith("set TBOT_TELEGRAM_CHAT_ID to -1009876")
    [check_down] = answer(502, {"ok": False})
    assert check_down.detail.startswith("unreachable: ")


def test_doctor_fails_a_book_whose_next_start_is_refused(tmp_path: Path) -> None:
    store = BarStore(tmp_path / "data")
    for symbol in ("BTCUSDT", "ETHUSDT"):
        bars = price_bars(T0, Timeframe.H4, [100.0] * 10, [100.0] * 10)
        store.write(symbol, Timeframe.H4, bars)
    config = PaperConfig(strategies=STRATEGIES, ledger=tmp_path / "paper.sqlite")
    with Ledger(config.ledger) as ledger:
        ledger.set_meta(BUDGET_META, repr(10000.0))
        ledger.add_fill(Fill(T0, "SOLUSDT", 5.0, 100.0, 0.0), 100.0)  # dropped from the config
    btc_only = config.model_copy(
        update={"strategies": [STRATEGIES[0].model_copy(update={"symbols": ["BTCUSDT"]})]}
    )

    async def offline(config: PaperConfig) -> list[Check]:
        async with httpx.AsyncClient() as client:  # what `tbot update` asks
            return await run_checks(config, settings(), client, store, offline=True)

    [(status, detail)] = by_name(asyncio.run(offline(btc_only)))["book"]
    assert status == "fail"
    assert detail.startswith("the book holds 5.00000000 SOLUSDT, which this config does not")
    cut = config.model_copy(update={"initial_cash": 1.0})  # the book is worth 10,000
    with Ledger(config.ledger) as ledger:
        ledger.add_fill(Fill(T0, "SOLUSDT", -5.0, 100.0, 0.0), 100.0)
        ledger.add_fill(Fill(T0, "BTCUSDT", 99.0, 100.0, 0.0), 100.0)
    with Ledger(config.ledger) as ledger:  # a fall: worth 9,900 at the stored 100, then cut
        ledger.add_adjustment(Adjustment(T0, "", 0.0, -9500.0, "withdrawn"))
    [(status, detail)] = by_name(asyncio.run(offline(cut)))["book"]
    assert status == "fail"
    assert detail.startswith("initial_cash 1.00 takes 9,999.00 out of a book worth")
    assert "book" not in by_name(asyncio.run(offline(config)))


def test_a_ledger_in_a_synced_folder_and_google_drive_are_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OneDrive", str(tmp_path / "OneDrive"))
    assert synced_folder(Path("G:/My Drive/Trading-Bot")) == "Google Drive"
    assert synced_folder(tmp_path / "My Drive" / "Trading-Bot") == "Google Drive"  # mirrored
    assert synced_folder(Path("G:/Shared drives/team/Trading-Bot")) == "Google Drive"
    monkeypatch.setattr(
        "tbot.live.doctor.synced_folder", lambda folder: "OneDrive" * ("OneDrive" in str(folder))
    )
    config = PaperConfig(strategies=STRATEGIES, ledger=tmp_path / "OneDrive" / "paper.sqlite")
    [(status, detail)] = by_name(check(config, settings(), fake()))["folder"]
    assert status == "warn"
    assert detail.startswith(f"the ledger {config.ledger} is in a folder OneDrive syncs")


def test_a_drive_running_low_is_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tbot.live.runner.shutil.disk_usage", lambda path: SimpleNamespace(free=512 * 1024**2)
    )
    config = PaperConfig(strategies=STRATEGIES, ledger=tmp_path / "paper.sqlite")
    [(status, detail)] = by_name(check(config, settings(), fake()))["disk"]
    assert status == "warn"
    assert detail.startswith("0.5 GB free on the drive of")


def test_env_lines_that_set_nothing_are_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "# Telegram\nTBOT_TELEGRAM_TOKEN=1:a\nTBOT_TELEGRAM_CHATID=42\nset TBOT_HEARTBEAT_URL=x\n"
        "export TBOT_BINANCE_API_KEY=k\nTBOT_SUPERVISED=1\n\n",
        "utf-8",
    )
    (tmp_path / ".env.txt").write_text("TBOT_TELEGRAM_CHAT_ID=42\n", "utf-8")
    config = PaperConfig(strategies=STRATEGIES, ledger=tmp_path / "paper.sqlite")
    found = [detail for _, detail in by_name(check(config, settings(), fake()))["env"]]
    assert found == [
        '.env.txt is there: tbot reads only .env (Notepad adds .txt; save it as "All files")',
        ".env line 3: TBOT_TELEGRAM_CHATID is no tbot setting (did you mean "
        "TBOT_TELEGRAM_CHAT_ID?)",
        ".env line 4 is not NAME=value, so it sets nothing",
    ]


def test_a_blocked_bot_fails_doctor(tmp_path: Path) -> None:
    config = live(tmp_path)
    found = by_name(check(config, settings(**ALERTS, **KEYS), fake(telegram_blocked=True)))
    [(status, detail)] = found["telegram"]
    assert status == "fail"
    assert detail.startswith("the bot may not write to the chat: unblock it in Telegram")


def test_a_misspelled_strategy_parameter_is_placed_and_named(tmp_path: Path) -> None:
    typo = STRATEGIES[0].model_copy(update={"params": {"entry": 55, "exti": 20}})
    config = PaperConfig(strategies=[typo], ledger=tmp_path / "paper.sqlite")
    [(status, detail)] = by_name(check(config, settings(), fake()))["strategies"]
    assert (status, detail) == (
        "fail",
        "donchian_trend: strategies.0.params.exti: unknown key (did you mean exit?)",
    )


def test_a_ping_doctor_sends_is_said_to_count(tmp_path: Path) -> None:
    config = PaperConfig(strategies=STRATEGIES, ledger=tmp_path / "paper.sqlite")
    values = settings(heartbeat_url="https://hc.example/ping")
    [(status, detail)] = by_name(check(config, values, fake()))["heartbeat"]
    assert (status, detail) == ("ok", "ping answered 200 (the monitor counts it as the bot alive)")
