"""Checks around real money: the budget, the alert channel, the key, whose turn Telegram is."""

import asyncio
import io
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from binance_spot_fake import DEFAULT_RULES, FakeSpot
from tbot.cli import (
    EXIT_ERROR,
    _alert,
    after_stop,
    amount_matches,
    announce,
    confirm_budget,
    main,
    quote_of,
    save_env,
)
from tbot.core.config import StrategyConfig
from tbot.core.models import Fill
from tbot.core.timeframe import Timeframe
from tbot.exchange.binance import TESTNET_URL, BinanceSpot
from tbot.live.clock import ServerClock
from tbot.live.commands import PollTurn, command_loop
from tbot.live.config import LiveConfig, Settings
from tbot.live.doctor import Check
from tbot.live.ledger import Ledger
from tbot.live.runner import (
    BUDGET_META,
    SUPERVISED_META,
    StartRefused,
    check_funds,
    check_key_rights,
    check_telegram,
    heartbeat_problem,
    session_label,
)
from tbot.monitoring.telegram import Telegram

STRATEGY = StrategyConfig(
    name="donchian_trend", symbols=["BTCUSDT", "ETHUSDT"], timeframe=Timeframe.H4, allocation=1.0
)
STRATEGY_YAML = "  - {name: donchian_trend, symbols: [BTCUSDT], timeframe: 4h, allocation: 1.0}\n"


def live(tmp_path: Path, mode: str = "live", **update: object) -> LiveConfig:
    return LiveConfig.model_validate(
        {
            "mode": mode,
            "initial_cash": 1000,
            "strategies": [STRATEGY],
            "ledger": tmp_path / f"{mode}.sqlite",
            **update,
        }
    )


def keys(**values: str) -> Settings:
    base = {"binance_api_key": "key", "binance_api_secret": "secret"}
    return Settings(_env_file=None, **{**base, **values})  # type: ignore[call-arg]


def funds(config: LiveConfig, spot: FakeSpot, ledger: Ledger) -> None:
    async def run() -> None:
        async with spot.client() as client:
            clock = ServerClock(httpx.Client(transport=httpx.MockTransport(spot.handle)))
            await check_funds(config, keys(), ledger, client, clock)

    asyncio.run(run())


def test_a_new_book_needs_its_budget_free_in_the_account(tmp_path: Path) -> None:
    config = live(tmp_path, initial_cash=10_000)  # a zero too many
    spot = FakeSpot(balances={"USDT": 1500.0}, prices={})
    with Ledger(config.ledger) as ledger:
        with pytest.raises(StartRefused, match=r"USDT 1,500\.00 free, and the bot's budget"):
            funds(config, spot, ledger)
        funds(live(tmp_path, initial_cash=1500), spot, ledger)  # all there: it starts
        funds(live(tmp_path, ownership="account", initial_cash=10_000), spot, ledger)


def test_a_raised_budget_needs_the_raise_free(tmp_path: Path) -> None:
    config = live(tmp_path, initial_cash=1000)
    spot = FakeSpot(balances={"USDT": 600.0}, prices={})
    with Ledger(config.ledger) as ledger:
        ledger.set_meta(BUDGET_META, repr(1000.0))
        funds(config, spot, ledger)  # unchanged: money the owner took out is reconciliation's
        ledger.add_fill(
            Fill(datetime(2024, 1, 1, tzinfo=UTC), "BTCUSDT", 0.01, 50000.0, 0.5), 50000
        )
        with pytest.raises(StartRefused, match=r"needs 1,499\.50"):  # 499.50 cash + 1000 more
            funds(live(tmp_path, initial_cash=2000), spot, ledger)
        funds(live(tmp_path, initial_cash=500), spot, ledger)  # a cut needs nothing


def test_cash_other_books_keep_on_the_account_is_not_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "first.yaml").write_text(
        "mode: testnet\ninitial_cash: 800\nledger: first.sqlite\nstrategies:\n"
        "  - {name: donchian_trend, symbols: [BTCUSDT, ETHUSDT], timeframe: 4h, allocation: 1}\n",
        "utf-8",
    )
    with Ledger(tmp_path / "first.sqlite") as first:
        first.set_meta(BUDGET_META, repr(800.0))
    config = live(tmp_path, "testnet", initial_cash=500, ledger=tmp_path / "second.sqlite")
    spot = FakeSpot(balances={"USDT": 1000.0}, prices={})
    with Ledger(config.ledger) as ledger:
        with pytest.raises(StartRefused, match=r"800\.00 of it the cash of first"):
            funds(config, spot, ledger)
        funds(live(tmp_path, "testnet", initial_cash=200, ledger=config.ledger), spot, ledger)
        ledger.add_order(datetime(2024, 1, 1, tzinfo=UTC), "BTCUSDT", 0.01, "pending", "", "x")
        funds(config, spot, ledger)  # an order in doubt: its cash is not known yet


def telegram_check(spot: FakeSpot, token: str = "t", chat: str = "1") -> None:
    async def run() -> None:
        async with spot.client() as client:
            await check_telegram(keys(telegram_token=token, telegram_chat_id=chat), client)

    asyncio.run(run())


def test_real_money_starts_only_when_telegram_takes_its_alerts() -> None:
    spot = FakeSpot(balances={}, prices={})
    telegram_check(spot)
    with pytest.raises(StartRefused, match="does not know TBOT_TELEGRAM_TOKEN"):
        telegram_check(spot, token="revoked")
    with pytest.raises(StartRefused, match="cannot send to TBOT_TELEGRAM_CHAT_ID"):
        telegram_check(spot, chat="99")
    spot.fail_next = [httpx.Response(502)]
    telegram_check(spot, token="revoked")  # Telegram down: alerts wait, the bot starts
    spot.raise_next = [httpx.ConnectError("no route")]
    telegram_check(spot, token="revoked")


def rights_notes(spot: FakeSpot) -> list[str]:
    async def run() -> list[str]:
        async with spot.client() as client:
            binance = BinanceSpot(client, spot.api_key, spot.secret, base_url=TESTNET_URL)
            return await check_key_rights(binance)

    return asyncio.run(run())


def test_needless_or_unreadable_key_rights_are_told() -> None:
    spot = FakeSpot(balances={}, prices={})
    assert rights_notes(spot) == []
    spot.restrictions = {**spot.restrictions, "enableFutures": True, "enableMargin": True}
    [note] = rights_notes(spot)
    assert note == (
        "the API key also allows margin trading, futures trading, which tbot never uses: "
        "turn it off"
    )
    spot.fail_next = [httpx.Response(400, json={"code": -1000, "msg": "unknown error"})]
    [note] = rights_notes(spot)
    assert note.startswith("could not read the API key's permissions")
    spot.restrictions = {**spot.restrictions, "enableWithdrawals": True}
    with pytest.raises(StartRefused, match="can withdraw"):
        rights_notes(spot)


def test_a_refusing_telegram_pauses_the_heartbeat_once_told() -> None:
    answers = [httpx.Response(403, json={"description": "bot was blocked by the user"})] * 2
    answers.append(httpx.Response(200, json={"ok": True}))
    told: list[str] = []

    async def run() -> list[str]:
        transport = httpx.MockTransport(lambda request: answers.pop(0))
        async with httpx.AsyncClient(transport=transport) as client:
            telegram = Telegram("t", "1", client)
            telegram.on_refused = told.append
            states = []
            for _ in range(3):
                await telegram.send("x")
                states.append(telegram.refused)
            return states

    states = asyncio.run(run())
    assert states[0].startswith("HTTP 403")
    assert states[2] == ""  # it took one again
    assert len(told) == 1  # recorded once, not for every alert
    assert heartbeat_problem([], "", None, states[0]).startswith("Telegram refuses alerts")
    assert heartbeat_problem([], "", None, "") == ""


def test_whose_turn_it_is_to_read_the_bot_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tbot.live.commands.TURN_DIR", tmp_path)
    paper, live_session, other = PollTurn("t", 0), PollTurn("t", 1), PollTurn("t", 0)
    assert paper.take()
    assert not other.take()  # first come
    assert not other.wanted()  # a session of the same rank never asks
    assert not live_session.take()  # held: it asks for the turn
    assert paper.wanted()
    assert not other.take()  # and nobody of a lower rank slips in meanwhile
    paper.release()
    assert live_session.take()
    assert not paper.wanted()  # its request is done
    another = PollTurn("other token", 0)
    assert another.take()  # another bot: another turn
    another.release()
    live_session.release()
    assert paper.take()
    paper.release()

    stuck = PollTurn("t", 0)
    (tmp_path / "a file").write_text("", encoding="utf-8")
    stuck.path = tmp_path / "a file" / "x.lock"  # cannot be created
    assert stuck.take()  # polls alone instead of never
    assert not stuck.holding

    paper.want.write_text("2", encoding="utf-8")
    assert not paper.take()  # `tbot notify` waits for it
    old = time.time() - 120
    os.utime(paper.want, (old, old))
    assert paper.take()  # a request left by a process that ended
    paper.release()


def test_a_session_gives_its_turn_after_confirming_what_it_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tbot.live.commands.TURN_DIR", tmp_path)
    monkeypatch.setattr("tbot.live.commands.TURN_SECONDS", 0.01)
    started = datetime(2024, 1, 1, tzinfo=UTC)
    polls: list[tuple[str | None, str | None]] = []
    first = [{"update_id": 7, "message": {"chat": {"id": 9}, "date": 0, "text": "/status"}}]

    async def handle(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        polls.append((params.get("offset"), params.get("timeout")))
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"ok": True, "result": first if len(polls) == 1 else []})

    async def echo(text: str) -> str:
        return text

    async def run() -> bool:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            turn = PollTurn("t", 0)
            task = asyncio.create_task(
                command_loop(Telegram("t", "1", client), echo, started, turn)
            )
            async with asyncio.timeout(5):
                while not polls:
                    await asyncio.sleep(0.01)
                waiting = PollTurn("t", 1)
                while not waiting.take():
                    await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            waiting.release()
            return turn.holding

    assert not asyncio.run(run())
    assert ("8", "0") in polls  # update 7 confirmed before letting go


def test_a_session_waiting_out_a_failure_lets_go_of_its_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tbot.live.commands.RETRY_SECONDS", 30)  # far beyond the test
    started = datetime(2024, 1, 1, tzinfo=UTC)

    async def handle(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.3)  # long enough for the other session to ask
        return httpx.Response(502)

    async def echo(text: str) -> str:
        return text

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            holder = PollTurn("t", 0)
            task = asyncio.create_task(
                command_loop(Telegram("t", "1", client), echo, started, holder)
            )
            waiting = PollTurn("t", 1)
            async with asyncio.timeout(5):
                while not holder.holding:
                    await asyncio.sleep(0.01)
                while not waiting.take():
                    await asyncio.sleep(0.05)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            waiting.release()

    asyncio.run(run())


def test_alerts_name_the_config_when_it_is_not_the_mode() -> None:
    assert session_label("live", "live") == "live"
    assert session_label("live", "") == "live"
    assert session_label("live", "btc_only") == "live btc_only"
    assert session_label("paper", "D:/bots/eth.yaml") == "paper eth"


def test_the_budget_is_typed_before_real_money_first_trades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = live(tmp_path, initial_cash=1000)
    typed: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt="": typed.pop(0))
    typed.append("10000")
    assert not confirm_budget(Path("config/live.yaml"), config)
    shown = capsys.readouterr().out
    where = Path("config/live.yaml")
    assert f"REAL MONEY: {where} lets the bot trade up to 1,000.00 USDT" in shown
    assert "after a 45% fall from the best value (about 450.00 of this budget)" in shown
    assert "`tbot halt live` sells everything" in shown
    typed.append("1,000 usdt")
    assert confirm_budget(Path("config/live.yaml"), config)

    with Ledger(config.ledger) as ledger:
        ledger.set_meta(BUDGET_META, repr(1000.0))
    assert confirm_budget(Path("config/live.yaml"), config)  # nothing new: no question
    assert typed == []
    typed.append("2000")
    assert confirm_budget(Path("config/live.yaml"), live(tmp_path, initial_cash=2000))
    assert "(initial_cash, raised from 1,000.00)" in capsys.readouterr().out
    typed.append("1000")
    assert not confirm_budget(Path("config/live.yaml"), live(tmp_path, initial_cash=2000))
    typed.append("3000")  # the budget means nothing here: it asks for "account"
    assert not confirm_budget(
        Path("config/live.yaml"), live(tmp_path, initial_cash=3000, ownership="account")
    )
    typed.append("account")
    account = live(tmp_path, initial_cash=3000, ownership="account")
    assert confirm_budget(Path("config/live.yaml"), account)
    assert "take over this whole Binance spot account (ownership: account)" in (
        capsys.readouterr().out
    )


def test_the_budget_asked_for_is_the_budget_in_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prompts: list[str] = []

    def answer(prompt: str = "") -> str:
        prompts.append(prompt)
        return prompt.split("(")[1].split(")")[0]  # exactly what it shows

    monkeypatch.setattr("builtins.input", answer)
    assert confirm_budget(Path("live.yaml"), live(tmp_path, initial_cash=1_234_567.89))
    assert prompts == ["To start, type the budget (1,234,567.89): "]


def test_amounts_match_as_people_type_them() -> None:
    assert amount_matches("1000", 1000.0)
    assert amount_matches(" 1,000.00 USDT ", 1000.0)
    assert amount_matches("1_000", 1000.0)
    assert not amount_matches("100", 1000.0)
    assert not amount_matches("yes", 1000.0)
    assert not amount_matches("", 1000.0)


def test_the_quote_currency_is_named_only_when_shared() -> None:
    config = live(Path("unused"))
    assert quote_of(config) == "USDT"
    mixed = [
        StrategyConfig(name="donchian_trend", symbols=[s], timeframe=Timeframe.H4, allocation=0.5)
        for s in ("ETHBTC", "BTCUSDT")
    ]
    assert quote_of(config.model_copy(update={"strategies": mixed})) != "USDT"


def test_a_real_money_start_says_so_and_names_the_panic_button(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    announce("live", Path("config/live.yaml"), live(tmp_path), Path("logs/live.jsonl"))
    err = capsys.readouterr().err
    assert "REAL MONEY: up to 1,000.00 USDT (initial_cash); `tbot halt live` sells" in err
    announce("testnet", Path("config/testnet.yaml"), live(tmp_path, "testnet"), Path("x.jsonl"))
    assert "REAL MONEY" not in capsys.readouterr().err


def test_a_wrong_budget_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "live.yaml"
    path.write_text(
        f"mode: live\ninitial_cash: 500\nledger: {(tmp_path / 'l.sqlite').as_posix()}\n"
        f"strategies:\n{STRATEGY_YAML}",
        "utf-8",
    )
    monkeypatch.setenv("TBOT_BINANCE_API_KEY", "k")
    monkeypatch.setenv("TBOT_BINANCE_API_SECRET", "s")
    monkeypatch.setattr("tbot.cli.interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "5000")
    assert main(["live", str(path), "--live"]) == EXIT_ERROR
    assert "that is not the budget in the config: nothing started" in capsys.readouterr().err
    assert not (tmp_path / "l.sqlite").exists()


def test_stop_says_a_scheduled_session_comes_back_and_what_it_holds(tmp_path: Path) -> None:
    path = tmp_path / "s.yaml"
    path.write_text(
        f"mode: testnet\nledger: {(tmp_path / 's.sqlite').as_posix()}\n"
        f"strategies:\n{STRATEGY_YAML}",
        "utf-8",
    )
    with Ledger(tmp_path / "s.sqlite") as ledger:
        ledger.set_meta(SUPERVISED_META, "0")
    assert after_stop(path, tmp_path / "s.sqlite") == ""
    with Ledger(tmp_path / "s.sqlite") as ledger:
        ledger.set_meta(SUPERVISED_META, "1")
        ledger.add_fill(Fill(datetime(2024, 1, 1, tzinfo=UTC), "BTCUSDT", 0.01, 50000.0, 0.5), 5e4)
    first, second = after_stop(path, tmp_path / "s.sqlite").splitlines()
    assert first.startswith("its scheduled task starts it again at the next boot")
    assert second == (
        f"its positions stay, with their exchange stops, until it runs again; "
        f"`tbot halt {path}` sells them"
    )


def test_keys_are_saved_only_once_binance_takes_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "live.yaml").write_text(
        f"mode: live\ninitial_cash: 1000\nledger: live.sqlite\nstrategies:\n{STRATEGY_YAML}",
        "utf-8",
    )
    old_key = "a" * 64
    (tmp_path / ".env").write_bytes(
        f"TBOT_BINANCE_API_KEY={old_key}\nTBOT_BINANCE_API_SECRET={'b' * 64}\n".encode()
    )
    entered = ["c" * 64]
    monkeypatch.setattr("tbot.cli.interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": entered.pop(0))
    monkeypatch.setattr("tbot.cli.getpass.getpass", lambda prompt="": "d" * 64)
    seen: list[Settings] = []
    verdict = [Check("fail", "permissions", "the key can withdraw; turn that off")]

    async def key_checks(config: LiveConfig, trial: Settings, client: object) -> list[Check]:
        seen.append(trial)
        return verdict

    async def works_on_testnet(key: str, secret: str, client: object) -> bool:
        return key == old_key

    monkeypatch.setattr("tbot.cli.key_checks", key_checks)
    monkeypatch.setattr("tbot.cli.works_on_testnet", works_on_testnet)
    assert main(["keys", "live"]) == EXIT_ERROR
    assert "not saved: fix what failed above" in capsys.readouterr().out
    assert seen[0].api_keys("live") == ("c" * 64, "d" * 64)
    assert old_key in (tmp_path / ".env").read_text(encoding="utf-8")  # untouched

    entered.append("c" * 64)
    verdict[:] = [Check("ok", "api key", "valid"), Check("fail", "budget", "deposit")]
    assert main(["keys", "live"]) == 0  # the key works; the budget is another matter
    out = capsys.readouterr().out
    assert "saved in .env as TBOT_BINANCE_API_KEY and TBOT_BINANCE_API_SECRET" in out
    assert "the testnet keys there before now are TBOT_BINANCE_TESTNET_API_KEY" in out
    saved = Settings(_env_file=tmp_path / ".env")  # type: ignore[call-arg]
    assert saved.api_keys("live") == ("c" * 64, "d" * 64)
    assert saved.api_keys("testnet") == (old_key, "b" * 64)  # testnet keeps working
    assert "d" * 64 not in out  # the secret is never echoed


def test_keys_need_a_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("tbot.cli.interactive", lambda: False)
    assert main(["keys", "testnet"]) == EXIT_ERROR
    assert "run `tbot keys` in a terminal" in capsys.readouterr().out


def test_an_old_key_the_testnet_cannot_check_stays_for_testnet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "live.yaml").write_text(
        f"mode: live\ninitial_cash: 1000\nledger: live.sqlite\nstrategies:\n{STRATEGY_YAML}",
        "utf-8",
    )
    old_key = "a" * 64
    (tmp_path / ".env").write_bytes(
        f"TBOT_BINANCE_API_KEY={old_key}\nTBOT_BINANCE_API_SECRET={'b' * 64}\n".encode()
    )
    monkeypatch.setattr("tbot.cli.interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "c" * 64)
    monkeypatch.setattr("tbot.cli.getpass.getpass", lambda prompt="": "d" * 64)
    monkeypatch.setattr("tbot.cli.env_readers", lambda path: ["Users"])

    async def key_checks(config: LiveConfig, trial: Settings, client: object) -> list[Check]:
        return [Check("ok", "api key", "valid")]

    async def unknown(key: str, secret: str, client: object) -> bool | None:
        return None  # the testnet could not be reached

    monkeypatch.setattr("tbot.cli.key_checks", key_checks)
    monkeypatch.setattr("tbot.cli.works_on_testnet", unknown)
    assert main(["keys", "live"]) == 0
    out = capsys.readouterr().out
    assert "could not be tried on the testnet; it stays as TBOT_BINANCE_TESTNET_API_KEY" in out
    assert "warning: .env can be read by Users: limit it to your account" in out
    assert "readable by your account only" not in out
    saved = Settings(_env_file=tmp_path / ".env")  # type: ignore[call-arg]
    assert saved.api_keys("testnet") == (old_key, "b" * 64)


def test_a_setting_the_environment_also_sets_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TBOT_TELEGRAM_CHAT_ID", "5")
    save_env(tmp_path / ".env", {"TBOT_TELEGRAM_CHAT_ID": "7", "TBOT_TELEGRAM_TOKEN": "t"})
    out = capsys.readouterr().out
    assert "the environment also sets TBOT_TELEGRAM_CHAT_ID, which wins over" in out
    assert "TBOT_TELEGRAM_TOKEN" not in out


def test_symbols_of_two_quote_assets_refuse_a_start(tmp_path: Path) -> None:
    mixed = StrategyConfig(
        name="donchian_trend",
        symbols=["BTCUSDT", "ETHBUSD"],
        timeframe=Timeframe.H4,
        allocation=1.0,
    )
    config = live(tmp_path, "testnet", strategies=[mixed])
    rules = {**DEFAULT_RULES, "ETHBUSD": DEFAULT_RULES["ETHUSDT"]}
    spot = FakeSpot(balances={"USDT": 5000.0}, prices={}, rules=rules)
    with Ledger(config.ledger) as ledger, pytest.raises(StartRefused, match="quoted in BUSD, USDT"):
        funds(config, spot, ledger)


def test_a_question_no_one_can_answer_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def closed(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("tbot.cli.interactive", lambda: True)
    monkeypatch.setattr("builtins.input", closed)
    assert main(["keys", "testnet"]) == EXIT_ERROR
    assert "no answer (the input ended): nothing changed" in capsys.readouterr().err


def test_a_name_the_console_cannot_show_ends_no_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    name = "b" + chr(0x416) + "t"  # a letter outside the code page of the console
    (tmp_path / "config" / f"{name}.yaml").write_text(
        f"ledger: x.sqlite\nstrategies:\n{STRATEGY_YAML}", "utf-8"
    )
    piped = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")  # strict, as on Windows
    monkeypatch.setattr("sys.stdout", piped)
    assert main(["status"]) == 0
    piped.flush()
    assert b"b?t" in piped.buffer.getvalue()


def test_a_supervised_start_that_gives_up_keeps_trying_its_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []
    answers: list[float | None] = [0.0, 0.0, None]  # no network twice, then sent

    async def deliver(self: Telegram, text: str) -> float | None:
        sent.append(text)
        return answers.pop(0)

    monkeypatch.setattr("tbot.cli.Telegram.deliver", deliver)
    monkeypatch.setattr("tbot.cli.ALERT_RETRY_WAITS", (0.0, 0.0, 0.0, 0.0))
    monkeypatch.setattr("tbot.cli.Settings", lambda: keys(telegram_token="t", telegram_chat_id="1"))
    monkeypatch.setattr("tbot.cli.supervised", lambda: True)
    _alert("tbot cannot start: a config error")
    assert len(sent) == 3  # its supervisor gives up: no later chance to say why
    monkeypatch.setattr("tbot.cli.supervised", lambda: False)
    answers[:] = [0.0, None]
    sent.clear()
    _alert("tbot cannot start: a config error")
    assert len(sent) == 1  # by hand, the terminal shows it
