"""Preflight checks: what would stop a session, endanger the account, or hide a failure."""

import asyncio
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import ValidationError

from tbot import __version__
from tbot.backtest.runner import run_period
from tbot.core.config import streams_of
from tbot.core.hints import validation_text
from tbot.core.timeframe import Timeframe
from tbot.data.http import describe_error
from tbot.data.store import BarStore, StoreUnreadable
from tbot.exchange.binance import (
    PRODUCTION_URL,
    TESTNET_URL,
    BinanceError,
    BinanceSpot,
)
from tbot.live.backup import daily_backups
from tbot.live.config import (
    ONE_QUOTE,
    LiveConfig,
    SessionConfig,
    Settings,
    env_notes,
    env_readers,
)
from tbot.live.ledger import Ledger, LedgerUnavailable
from tbot.live.release import newest_release, update_note
from tbot.live.runner import (
    HEARTBEAT_URL_HINT,
    KEYS_MISSING,
    AlreadyRunning,
    StartRefused,
    budget_change,
    halt_path,
    instance_lock,
    key_hint,
    leftovers,
    load_guard,
    low_disk,
    make_spot,
    mode_problem,
    needless_rights,
    other_books_cash,
    quote_problem,
    restore_portfolio,
    stop_path,
    symbols_of,
    unbooked_budget,
    valid_url,
)
from tbot.monitoring.telegram import Telegram, migrated_to
from tbot.portfolio.allocation import build_slots

MAX_CLOCK_OFFSET = 3.0  # seconds; the bot corrects for it, but a drifting clock needs a fix
# Failures a session starts despite: offline they warn, so `tbot update` restarts a bot
# that ran before it (only what stops a start keeps it down).
STARTS_ANYWAY = frozenset({"telegram", "heartbeat", "kill switch"})
MIN_GUARD_DAYS = 365  # history the kill switch check needs to say anything
BACKUP_BEHIND_DAYS = 2  # a running session copies the ledger every 6 hours

Status = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class Check:
    status: Status
    name: str
    detail: str


async def run_checks(
    config: SessionConfig,
    settings: Settings,
    client: httpx.AsyncClient,
    store: BarStore | None = None,
    *,
    offline: bool = False,
) -> list[Check]:
    """Offline leaves out what depends on the network, which a running bot retries, and
    fails only on what stops a session from starting."""
    real_money = isinstance(config, LiveConfig) and config.mode == "live"
    checks = strategy_checks(config) + ledger_checks(config) + alert_checks(settings, real_money)
    if isinstance(config, LiveConfig) and settings.api_keys(config.mode) is None:
        checks.append(Check("fail", "api key", KEYS_MISSING))
    if store is not None:
        checks.extend(start_checks(config, store))
    checks.extend(folder_checks(config, store))
    env = Path(".env")
    readers = env_readers(env) if env.is_file() else []
    if readers:
        checks.append(
            Check(
                "warn",
                "env",
                f".env, with the keys, can be read by {', '.join(readers)}: limit it to "
                "your account (README, Security)",
            )
        )
    checks.extend(env_checks(env))
    if offline:  # a real-money start refuses a missing Telegram, offline or not
        anyway = STARTS_ANYWAY - {"telegram"} if real_money else STARTS_ANYWAY
        checks = [
            Check("warn", c.name, c.detail) if c.status == "fail" and c.name in anyway else c
            for c in checks
        ]
    if checks[0].status == "fail":
        return checks  # nothing below means much for a config that cannot start
    if settings.heartbeat_url and valid_url(settings.heartbeat_url) and not offline:
        checks.append(await heartbeat_check(settings.heartbeat_url, client))
    if store is not None:
        checks.append(await asyncio.to_thread(guard_check, config, store))
    if offline:
        return checks
    live = config if isinstance(config, LiveConfig) else None
    public = BinanceSpot(client, "", "", base_url=PRODUCTION_URL)
    try:
        offset = await server_offset(public)
    except (httpx.HTTPError, BinanceError) as exc:
        checks.append(unreachable(exc))
        return checks
    side = "behind" if offset > 0 else "ahead of"  # offset: Binance minus this clock
    checks.append(
        Check(
            "ok" if abs(offset) <= MAX_CLOCK_OFFSET else "warn",
            "clock",
            f"this computer is {abs(offset):.2f} s {side} Binance"
            + (
                ""
                if abs(offset) <= MAX_CLOCK_OFFSET
                else "; turn on time sync (Windows: Settings, Time & language, Sync now)"
            ),
        )
    )
    if live is None:
        checks.extend(await symbol_checks(config, public))
    else:
        checks.extend(await account_checks(live, settings, client, offset))
    if settings.telegram_token and settings.telegram_chat_id:
        verified = await telegram_check(settings.telegram_token, settings.telegram_chat_id, client)
        checks = [verified if c.name == "telegram" else c for c in checks]
    note = update_note(await asyncio.to_thread(newest_release))
    if note:
        checks.append(Check("warn", "version", note))
    return checks


# Folder names of cloud services: Google Drive for desktop streams "G:\My Drive" and
# "G:\Shared drives", or mirrors "%USERPROFILE%\My Drive".
SYNCED = {
    "onedrive": "OneDrive",
    "dropbox": "Dropbox",
    "google drive": "Google Drive",
    "my drive": "Google Drive",
    "shared drives": "Google Drive",
    "iclouddrive": "iCloud Drive",
    "icloud drive": "iCloud Drive",
}
LOW_DISK_GB = 2.0


def synced_folder(folder: Path) -> str:
    """The cloud service that syncs this folder, or ""."""
    roots = [
        os.environ.get(name, "") for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")
    ]
    parts = [part.lower() for part in folder.resolve().parts]
    for root in filter(None, roots):
        top = [part.lower() for part in Path(root).resolve().parts]
        if parts[: len(top)] == top:  # inside it, not a sibling such as OneDriveArchive
            return "OneDrive"
    for lower, name in SYNCED.items():  # also "OneDrive - Company", "Dropbox (Personal)"
        if any(part == lower or part.startswith((f"{lower} - ", f"{lower} (")) for part in parts):
            return name
    return ""


def folder_checks(config: SessionConfig, store: BarStore | None) -> list[Check]:
    """Folders a cloud service syncs (the repository with .env, or the ledger), and a drive
    running out of room."""
    checks = []
    synced = synced_folder(Path.cwd())
    if synced:
        checks.append(
            Check(
                "warn",
                "folder",
                f"the repository is in a folder {synced} syncs: it uploads .env with the keys, "
                "and syncing a running ledger can damage it; move the repository out",
            )
        )
    else:
        synced = synced_folder(config.ledger.parent)
        if synced:
            checks.append(
                Check(
                    "warn",
                    "folder",
                    f"the ledger {config.ledger} is in a folder {synced} syncs, which can "
                    "damage it while a session writes it: give the config a ledger outside it",
                )
            )
    places = [config.ledger.parent] + ([store.base] if store is not None else [])
    low = low_disk([place for place in places if place.exists()])
    if low:
        checks.append(Check("warn", "disk", f"{low}; a full disk stops the bot: make room"))
    return checks


def env_checks(env: Path) -> list[Check]:
    """Lines of .env that set nothing (a misspelled name, another shell's syntax), and a
    copy Notepad saved as .env.txt."""
    checks = []
    twin = env.with_name(env.name + ".txt")
    if twin.is_file():
        checks.append(
            Check(
                "warn",
                "env",
                f"{twin} is there: tbot reads only {env} (Notepad adds .txt; save it as "
                '"All files")',
            )
        )
    try:
        text = env.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):  # missing, or said when it is read
        return checks
    checks.extend(
        Check("warn", "env", f"{env} {note}") for note in env_notes(text, Settings.names())
    )
    return checks


def start_checks(config: SessionConfig, store: BarStore) -> list[Check]:
    """What refuses the next start of this book for good: coins of a pair the config no
    longer trades, or a budget cut beyond the book's value."""
    if not config.ledger.is_file():
        return []
    now = datetime.now(UTC)
    try:
        with Ledger(config.ledger) as ledger:
            if mode_problem(config, ledger):
                return []  # the ledger check says so
            _, problem = leftovers(config, restore_portfolio(config, ledger), store, now)
            _, _, budget = budget_change(config, ledger, store, now)
    except LedgerUnavailable:
        return []  # the ledger check says so
    return [Check("fail", "book", text) for text in (problem, budget) if text]


async def server_offset(public: BinanceSpot) -> float:
    """Binance's clock minus this computer's, in seconds. Raises httpx and Binance errors."""
    before = time.time()
    server = await public.server_time()
    return server.timestamp() - (before + time.time()) / 2


def unreachable(exc: Exception) -> Check:
    return Check(
        "fail",
        "binance",
        f"unreachable: {exc!r}; check the network (HTTP 403 or 451: Binance may not serve "
        "this region)",
    )


async def key_checks(
    config: LiveConfig, settings: Settings, client: httpx.AsyncClient
) -> list[Check]:
    """What `tbot keys` asks Binance before it saves a key: that it signs, trades, and for
    real money cannot withdraw."""
    try:
        offset = await server_offset(BinanceSpot(client, "", "", base_url=PRODUCTION_URL))
    except (httpx.HTTPError, BinanceError) as exc:
        return [unreachable(exc)]
    return await account_checks(config, settings, client, offset)


async def works_on_testnet(key: str, secret: str, client: httpx.AsyncClient) -> bool | None:
    """Whether Binance's testnet takes this key pair; None when it cannot be reached."""
    public = BinanceSpot(client, "", "", base_url=TESTNET_URL)
    try:
        offset = await server_offset(public)

        def clock() -> datetime:
            return datetime.now(UTC) + timedelta(seconds=offset)

        await BinanceSpot(client, key, secret, base_url=TESTNET_URL, clock=clock).account()
    except httpx.HTTPError:
        return None
    except BinanceError as exc:  # a refusal says no; an outage or a rate limit cannot tell
        return False if 400 <= exc.status < 500 and exc.status not in (418, 429) else None
    return True


async def telegram_check(token: str, chat: str, client: httpx.AsyncClient) -> Check:
    """Ask Telegram whether it knows the token and the chat; nothing is sent."""
    bot = Telegram(token, chat, client)
    try:
        await bot.me()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 404):
            return Check(
                "fail", "telegram", "Telegram does not know TBOT_TELEGRAM_TOKEN (`tbot notify`)"
            )
        return Check("fail", "telegram", f"unreachable: {describe_error(exc)}")
    except httpx.HTTPError as exc:  # the token is in the URL: never print it
        return Check("fail", "telegram", f"unreachable: {describe_error(exc)}")
    try:
        await bot.chat()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code >= 500 or exc.response.status_code == 429:
            return Check("fail", "telegram", f"unreachable: {describe_error(exc)}")
        moved = migrated_to(exc.response)
        if moved:
            return Check(
                "fail",
                "telegram",
                f"the group became a supergroup: set TBOT_TELEGRAM_CHAT_ID to {moved}",
            )
        return Check(
            "fail",
            "telegram",
            "the bot cannot reach TBOT_TELEGRAM_CHAT_ID: a wrong id, or the bot is not in that "
            "chat (`tbot notify` finds it)",
        )
    except httpx.HTTPError as exc:
        return Check("fail", "telegram", f"unreachable: {describe_error(exc)}")
    try:
        await bot.typing()  # getChat still answers for a bot the owner blocked
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code >= 500 or exc.response.status_code == 429:
            return Check("fail", "telegram", f"unreachable: {describe_error(exc)}")
        return Check(
            "fail",
            "telegram",
            "the bot may not write to the chat: unblock it in Telegram (or add it to the group "
            "again), then `tbot notify`",
        )
    except httpx.HTTPError as exc:
        return Check("fail", "telegram", f"unreachable: {describe_error(exc)}")
    return Check("ok", "telegram", "Telegram knows the bot and the chat; `tbot notify` tests it")


def strategy_checks(config: SessionConfig) -> list[Check]:
    for index, strategy in enumerate(config.strategies):
        try:
            build_slots([strategy])
        except ValidationError as exc:
            where = f"strategies.{index}.params."
            return [Check("fail", "strategies", f"{strategy.name}: {validation_text(exc, where)}")]
        except ValueError as exc:
            return [Check("fail", "strategies", str(exc))]
    slots = build_slots(config.strategies)
    names = ", ".join(f"{s.strategy.name} {s.timeframe}" for s in slots)
    checks = [Check("ok", "strategies", names)]
    finest = min((s.timeframe for s in slots), key=lambda t: t.millis)
    if finest.millis < Timeframe.M15.millis:
        checks.append(
            Check(
                "warn",
                "timeframe",
                f"{finest} bars: every closed bar rewrites a year of stored bars, and the "
                "ledger grows by about 150 MB a year; sessions are made for 15m bars and up",
            )
        )
    return checks


def guard_check(config: SessionConfig, store: BarStore) -> Check:
    """Would the kill switch have stopped this config on the stored history?"""
    try:
        return _guard_check(config, store)
    except StoreUnreadable as exc:  # a damaged file: named, and the report goes on
        return Check("fail", "market data", str(exc))


def _guard_check(config: SessionConfig, store: BarStore) -> Check:
    if not config.guard.max_drawdown:
        return Check("warn", "kill switch history", "off (guard.max_drawdown is 0)")
    firsts = [store.first_open_time(s, c.timeframe) for c in config.strategies for s in c.symbols]
    hint = store.download_hint(streams_of(config))
    if any(first is None for first in firsts):
        return Check("warn", "kill switch history", f"no stored history to test it on; run {hint}")
    start = max(f for f in firsts if f is not None) + timedelta(days=90)  # after the warmup
    lasts = [store.last_open_time(s, c.timeframe) for c in config.strategies for s in c.symbols]
    days = (min(last for last in lasts if last is not None) - start).days
    if days < MIN_GUARD_DAYS:
        return Check(
            "warn",
            "kill switch history",
            f"too little stored history to test it ({max(days, 0)} days after the warmup); "
            f"run {hint} for the full history",
        )
    try:
        result = run_period(config, store, start, None, config.guard)
    except ValueError as exc:
        return Check("warn", "kill switch history", f"could not test it on history: {exc}")
    limit = f"{config.guard.max_drawdown:.0%}"
    if result.halted:
        return Check(
            "warn",
            "kill switch history",
            f"at {limit} it would have halted the backtest on {result.halted}; "
            "a strategy's normal drawdowns should stay inside it",
        )
    return Check(
        "ok", "kill switch history", f"{limit} never tripped on history since {start:%Y-%m-%d}"
    )


async def heartbeat_check(url: str, client: httpx.AsyncClient) -> Check:
    try:
        response = await client.get(url, timeout=10.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:  # the URL is a secret: anyone with it can ping
        return Check("fail", "heartbeat", f"ping failed: {describe_error(exc)}")
    return Check(
        "ok",
        "heartbeat",
        f"ping answered {response.status_code} (the monitor counts it as the bot alive)",
    )


def backup_check(config: SessionConfig) -> Check:
    """Run before the ledger is opened: opening it touches its files."""
    if not config.backup_days:
        return Check("warn", "backup", "off (backup_days is 0)")
    backups = daily_backups(config.ledger)
    if not backups:
        return Check("warn", "backup", "none yet; a running session copies the ledger every 6 h")
    files = [config.ledger, config.ledger.with_name(config.ledger.name + "-wal")]
    changed = max(p.stat().st_mtime for p in files if p.exists())
    behind = (changed - backups[-1].stat().st_mtime) / 86400
    if behind > BACKUP_BEHIND_DAYS:
        return Check(
            "warn", "backup", f"{backups[-1]} is {behind:.0f} days older than the ledger's changes"
        )
    copies = "1 daily copy" if len(backups) == 1 else f"{len(backups)} daily copies"
    return Check("ok", "backup", f"{copies}, newest {backups[-1]}")


def ledger_checks(config: SessionConfig) -> list[Check]:
    if config.ledger.exists() and not config.ledger.is_file():
        return [Check("fail", "ledger", f"{config.ledger} is not a file")]
    if not config.ledger.is_file():
        return [Check("ok", "ledger", f"none yet; the first start creates {config.ledger}")]
    backup = backup_check(config)
    try:
        ledger = Ledger(config.ledger)
    except LedgerUnavailable as exc:
        return [Check("fail", "ledger", str(exc))]
    checks = []
    with ledger:
        guard = load_guard(config, ledger)
        point = ledger.latest_equity()
        summary = f"{len(ledger.fills())} fills"
        if point is not None:
            summary = f"equity {point.equity:,.2f} at {point.time:%Y-%m-%d %H:%M} UTC, " + summary
        problem = mode_problem(config, ledger)
        if problem:
            checks.append(Check("fail", "ledger", problem))
        else:
            checks.append(Check("ok", "ledger", f"{config.ledger}: {summary}"))
        if guard.state.halted:
            reason = guard.state.halt_reason
            checks.append(Check("fail", "kill switch", f"halted: {reason}; run `tbot resume`"))
        doubtful = ledger.unresolved_orders()
        if doubtful:
            checks.append(
                Check("warn", "orders", f"{len(doubtful)} in doubt; the next start settles them")
            )
    try:
        with instance_lock(config.ledger):
            pass
    except AlreadyRunning:
        checks.append(Check("ok", "process", "a session is running on this ledger now"))
    else:
        if stop_path(config.ledger).exists():
            checks.append(
                Check(
                    "warn",
                    "stop",
                    "a `tbot stop` request waits: a supervised start within 20 minutes of it "
                    "stops at once (`tbot stop --cancel` withdraws it)",
                )
            )
        if halt_path(config.ledger).exists():
            checks.append(
                Check(
                    "warn",
                    "halt",
                    "a `tbot halt` request waits: the next start sells what the bot holds and "
                    "halts (`tbot resume` withdraws it)",
                )
            )
    return [*checks, backup]


def alert_checks(settings: Settings, real_money: bool = False) -> list[Check]:
    """A real-money bot must be able to say that it failed: no Telegram fails it."""
    token, chat = settings.telegram_token, settings.telegram_chat_id
    if token and chat:
        telegram = Check("ok", "telegram", "configured; `tbot notify` sends a test message")
    elif token or chat:
        telegram = Check(
            "fail", "telegram", "set both TBOT_TELEGRAM_TOKEN and _CHAT_ID (`tbot notify` helps)"
        )
    else:
        telegram = (
            Check(
                "fail",
                "telegram",
                "not configured: a real-money bot must be able to send alerts (`tbot notify`)",
            )
            if real_money
            else Check(
                "warn",
                "telegram",
                "not configured: alerts only reach the log (`tbot notify` sets it up)",
            )
        )
    url = settings.heartbeat_url
    if not url:
        return [
            telegram,
            Check("warn", "heartbeat", "not configured: nobody hears about a dead bot"),
        ]
    if not valid_url(url):
        return [
            telegram,
            Check("fail", "heartbeat", HEARTBEAT_URL_HINT),
        ]
    return [telegram]  # pinged once the network is known to work


async def symbol_checks(config: SessionConfig, spot: BinanceSpot) -> list[Check]:
    symbols = symbols_of(config)
    try:
        rules = await spot.load_rules(symbols)
    except BinanceError as exc:
        return [Check("fail", "symbols", exc.message)]
    except httpx.HTTPError as exc:
        return [Check("fail", "symbols", f"unreachable: {describe_error(exc)}")]
    quotes = sorted({rule.quote for rule in rules.values()})
    if len(quotes) > 1:
        return [Check("fail", "symbols", f"quoted in {', '.join(quotes)}: {ONE_QUOTE}")]
    problem = quote_problem(config, rules)
    if problem:  # a testnet or live start refuses it; paper trades nothing
        return [Check("fail" if isinstance(config, LiveConfig) else "warn", "symbols", problem)]
    paused = [f"{s} {spot.status[s]}" for s in sorted(rules) if not spot.trading(s)]
    if paused:
        return [
            Check(
                "warn",
                "symbols",
                f"Binance does not trade {', '.join(paused)} now; its orders wait until it does",
            )
        ]
    small = [
        f"{symbol} {rule.min_notional}"
        for symbol, rule in sorted(rules.items())
        if config.rebalance.min_notional < float(rule.min_notional)
    ]
    if small:
        return [
            Check(
                "warn",
                "symbols",
                "rebalance.min_notional is below the exchange minimum (" + ", ".join(small) + ")",
            )
        ]
    return [Check("ok", "symbols", f"{', '.join(sorted(rules))} trading")]


async def account_checks(
    config: LiveConfig, settings: Settings, client: httpx.AsyncClient, offset: float
) -> list[Check]:
    try:
        spot = make_spot(
            config, settings, client, lambda: datetime.now(UTC) + timedelta(seconds=offset)
        )
    except StartRefused:
        url = TESTNET_URL if config.mode == "testnet" else PRODUCTION_URL
        symbols = await symbol_checks(config, BinanceSpot(client, "", "", base_url=url))
        return symbols  # the missing keys are reported above
    checks = await symbol_checks(config, spot)
    try:
        account = await spot.account()
    except httpx.HTTPError as exc:
        return [*checks, Check("fail", "api key", f"unreachable: {describe_error(exc)}")]
    except BinanceError as exc:
        if exc.status >= 500 or exc.status == 429:
            return [*checks, Check("fail", "api key", f"unreachable: {exc}")]
        hint = key_hint(exc, settings.key_names(config.mode))
        hint = f"; {hint}" if hint else ""
        return [*checks, Check("fail", "api key", f"rejected: {exc}{hint}")]
    checks.append(
        Check("ok", "api key", f"valid on {spot.base_url}")
        if account.get("canTrade", False)
        else Check("fail", "api key", "the account cannot trade")
    )
    if config.mode == "live":
        checks.extend(await permission_checks(spot))
    if spot.rules:
        quote = next(iter(spot.rules.values())).quote
        bases = sorted({rules.base for rules in spot.rules.values()})
        checks.append(budget_check(config, account, quote, bases))
    if config.protective_stop_pct == 0:
        checks.append(Check("warn", "stops", "protective stops are off: a dead bot has no exit"))
    if config.mode == "live":
        checks.append(Check("ok", "mode", "real money; `tbot live` needs --live"))
    return checks


async def permission_checks(spot: BinanceSpot) -> list[Check]:
    try:
        rights = await spot.api_restrictions()
    except (httpx.HTTPError, BinanceError) as exc:
        return [Check("warn", "permissions", f"cannot read them: {exc}")]
    checks = []
    if rights.get("enableWithdrawals", True):
        checks.append(Check("fail", "permissions", "the key can withdraw; turn that off"))
    if not rights.get("enableSpotAndMarginTrading", False):
        checks.append(Check("fail", "permissions", "the key cannot trade spot; turn that on"))
    extra = needless_rights(rights)
    if extra:
        checks.append(
            Check("warn", "permissions", f"the key also allows {extra}: tbot never uses it")
        )
    if not rights.get("ipRestrict", False):
        checks.append(
            Check(
                "warn", "permissions", "the key works from any IP; restrict it if yours is static"
            )
        )
    return checks or [Check("ok", "permissions", "trading on, withdrawals off, IP restricted")]


def budget_check(
    config: LiveConfig, account: dict[str, Any], quote: str, bases: Sequence[str] = ()
) -> Check:
    rows = account.get("balances", [])
    free = sum(float(row["free"]) for row in rows if row.get("asset") == quote)
    if config.ownership == "account":
        held = {
            str(row.get("asset")): float(row["free"]) + float(row.get("locked", 0))
            for row in rows
            if row.get("asset") in (quote, *bases)
        }
        named = ", ".join(f"{asset} {amount:,.8g}" for asset, amount in held.items() if amount)
        return Check(
            "warn",
            "budget",
            f"ownership account: the bot takes over and trades all of {named or 'nothing yet'}, "
            "not only initial_cash; use it only on an account kept for the bot",
        )
    needed = config.initial_cash
    if config.ledger.is_file():
        with Ledger(config.ledger) as ledger:  # plus a budget change the next start books
            needed = restore_portfolio(config, ledger).cash + unbooked_budget(config, ledger)
    taken, books = other_books_cash(config)  # the same free cash, counted once
    theirs = f" ({taken:,.2f} of it the cash of {', '.join(books)})" if books else ""
    if free - taken + 1e-9 < needed:
        return Check(
            "fail",
            "budget",
            f"{quote} {free:,.2f} free{theirs}, the bot needs {needed:,.2f}; deposit or lower "
            "initial_cash",
        )
    return Check("ok", "budget", f"{quote} {free:,.2f} free{theirs} covers the bot's {needed:,.2f}")


def checks_text(checks: list[Check]) -> str:
    lines = [f"tbot {__version__}"]
    lines += [f"{c.status.upper():<5}{c.name}: {c.detail}" for c in checks]
    failed = sum(c.status == "fail" for c in checks)
    warned = sum(c.status == "warn" for c in checks)
    lines.append(f"{_count(failed, 'problem')}, {_count(warned, 'warning')}")
    return "\n".join(lines)


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"
