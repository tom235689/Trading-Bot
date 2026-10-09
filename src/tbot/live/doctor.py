"""Preflight checks: what would stop a session, endanger the account, or hide a failure."""

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from pydantic import ValidationError

from tbot.backtest.runner import run_period
from tbot.data.store import BarStore
from tbot.exchange.binance import PRODUCTION_URL, TESTNET_URL, BinanceError, BinanceSpot
from tbot.live.backup import daily_backups
from tbot.live.config import LiveConfig, SessionConfig, Settings
from tbot.live.ledger import Ledger, LedgerUnavailable
from tbot.live.runner import (
    AlreadyRunning,
    instance_lock,
    load_guard,
    make_spot,
    restore_portfolio,
    symbols_of,
    unbooked_budget,
    valid_url,
)
from tbot.portfolio.allocation import build_slots

MAX_CLOCK_OFFSET = 1.0  # seconds; the bot corrects for it, but a drifting clock needs a fix
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
    """Offline leaves out what depends on the network, which a running bot retries."""
    checks = strategy_checks(config) + ledger_checks(config) + alert_checks(settings)
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
        before = time.time()
        server = await public.server_time()
        offset = server.timestamp() - (before + time.time()) / 2
    except (httpx.HTTPError, BinanceError) as exc:
        checks.append(Check("fail", "binance", f"unreachable: {exc!r}; check the network"))
        return checks
    checks.append(
        Check(
            "ok" if abs(offset) <= MAX_CLOCK_OFFSET else "warn",
            "clock",
            f"{offset:+.2f} s against Binance"
            + ("" if abs(offset) <= MAX_CLOCK_OFFSET else "; turn on Windows time sync"),
        )
    )
    if live is None:
        checks.extend(await symbol_checks(config, public))
    else:
        checks.extend(await account_checks(live, settings, client, offset))
    return checks


def strategy_checks(config: SessionConfig) -> list[Check]:
    try:
        slots = build_slots(config.strategies)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        return [Check("fail", "strategies", details)]
    except ValueError as exc:
        return [Check("fail", "strategies", str(exc))]
    names = ", ".join(f"{s.strategy.name} {s.timeframe}" for s in slots)
    return [Check("ok", "strategies", names)]


def guard_check(config: SessionConfig, store: BarStore) -> Check:
    """Would the kill switch have stopped this config on the stored history?"""
    if not config.guard.max_drawdown:
        return Check("warn", "kill switch", "off (guard.max_drawdown is 0)")
    firsts = [store.first_open_time(s, c.timeframe) for c in config.strategies for s in c.symbols]
    if any(first is None for first in firsts):
        return Check("warn", "kill switch", "no stored history to test it on; run `tbot download`")
    start = max(f for f in firsts if f is not None) + timedelta(days=90)  # after the warmup
    lasts = [store.last_open_time(s, c.timeframe) for c in config.strategies for s in c.symbols]
    days = (min(last for last in lasts if last is not None) - start).days
    if days < MIN_GUARD_DAYS:
        return Check(
            "warn",
            "kill switch",
            f"too little stored history to test it ({max(days, 0)} days after the warmup); "
            "run `tbot download` for the full history",
        )
    try:
        result = run_period(config, store, start, None, config.guard)
    except ValueError as exc:
        return Check("warn", "kill switch", f"could not test it on history: {exc}")
    limit = f"{config.guard.max_drawdown:.0%}"
    if result.halted:
        return Check(
            "warn",
            "kill switch",
            f"at {limit} it would have halted the backtest on {result.halted}; "
            "a strategy's normal drawdowns should stay inside it",
        )
    return Check("ok", "kill switch", f"{limit} never tripped on history since {start:%Y-%m-%d}")


async def heartbeat_check(url: str, client: httpx.AsyncClient) -> Check:
    try:
        response = await client.get(url, timeout=10.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return Check("fail", "heartbeat", f"ping failed: {exc!r}")
    return Check("ok", "heartbeat", f"ping answered {response.status_code}")


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
    return Check("ok", "backup", f"{len(backups)} daily copies, newest {backups[-1]}")


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
        checks.append(Check("warn", "process", "a session is running on this ledger now"))
    return [*checks, backup]


def alert_checks(settings: Settings) -> list[Check]:
    token, chat = settings.telegram_token, settings.telegram_chat_id
    if token and chat:
        telegram = Check("ok", "telegram", "configured; `tbot notify` sends a test message")
    elif token or chat:
        telegram = Check(
            "fail", "telegram", "set both TBOT_TELEGRAM_TOKEN and _CHAT_ID (`tbot notify` helps)"
        )
    else:
        telegram = Check(
            "warn",
            "telegram",
            "not configured: alerts only reach the log (`tbot notify` sets it up)",
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
            Check("fail", "heartbeat", "TBOT_HEARTBEAT_URL must be a full http(s):// URL"),
        ]
    return [telegram]  # pinged once the network is known to work


async def symbol_checks(config: SessionConfig, spot: BinanceSpot) -> list[Check]:
    try:
        rules = await spot.load_rules(symbols_of(config))
    except BinanceError as exc:
        return [Check("fail", "symbols", exc.message)]
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
    except ValueError as exc:
        url = TESTNET_URL if config.mode == "testnet" else PRODUCTION_URL
        symbols = await symbol_checks(config, BinanceSpot(client, "", "", base_url=url))
        return [*symbols, Check("fail", "api key", str(exc))]
    checks = await symbol_checks(config, spot)
    try:
        account = await spot.account()
    except (httpx.HTTPError, BinanceError) as exc:
        return [*checks, Check("fail", "api key", f"rejected: {exc}")]
    checks.append(
        Check("ok", "api key", f"valid on {spot.base_url}")
        if account.get("canTrade", False)
        else Check("fail", "api key", "the account cannot trade")
    )
    if config.mode == "live":
        checks.extend(await permission_checks(spot))
    if spot.rules:
        quote = next(iter(spot.rules.values())).quote
        checks.append(budget_check(config, account, quote))
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
    if not rights.get("ipRestrict", False):
        checks.append(
            Check(
                "warn", "permissions", "the key works from any IP; restrict it if yours is static"
            )
        )
    return checks or [Check("ok", "permissions", "trading on, withdrawals off, IP restricted")]


def budget_check(config: LiveConfig, account: dict[str, Any], quote: str) -> Check:
    rows = account.get("balances", [])
    free = sum(float(row["free"]) for row in rows if row.get("asset") == quote)
    if config.ownership == "account":
        return Check(
            "warn",
            "budget",
            f"ownership account: the bot manages every asset ({quote} {free:,.2f} free)",
        )
    needed = config.initial_cash
    if config.ledger.is_file():
        with Ledger(config.ledger) as ledger:  # plus a budget change the next start books
            needed = restore_portfolio(config, ledger).cash + unbooked_budget(config, ledger)
    if free + 1e-9 < needed:
        return Check(
            "fail",
            "budget",
            f"{quote} {free:,.2f} free, the bot needs {needed:,.2f}; deposit or lower initial_cash",
        )
    return Check("ok", "budget", f"{quote} {free:,.2f} free covers the bot's {needed:,.2f}")


def checks_text(checks: list[Check]) -> str:
    lines = [f"{c.status.upper():<5}{c.name}: {c.detail}" for c in checks]
    failed = sum(c.status == "fail" for c in checks)
    warned = sum(c.status == "warn" for c in checks)
    lines.append(f"{_count(failed, 'problem')}, {_count(warned, 'warning')}")
    return "\n".join(lines)


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"
