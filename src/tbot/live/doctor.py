"""Preflight checks: what would stop a session, endanger the account, or hide a failure."""

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx

from tbot.exchange.binance import PRODUCTION_URL, BinanceError, BinanceSpot
from tbot.live.config import LiveConfig, SessionConfig, Settings
from tbot.live.ledger import Ledger, LedgerUnavailable
from tbot.live.runner import (
    AlreadyRunning,
    instance_lock,
    load_guard,
    make_spot,
    restore_portfolio,
    symbols_of,
)

MAX_CLOCK_OFFSET = 1.0  # seconds; the bot corrects for it, but a drifting clock needs a fix

Status = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class Check:
    status: Status
    name: str
    detail: str


async def run_checks(
    config: SessionConfig, settings: Settings, client: httpx.AsyncClient
) -> list[Check]:
    checks = ledger_checks(config) + alert_checks(settings)
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


def ledger_checks(config: SessionConfig) -> list[Check]:
    if not config.ledger.is_file():
        return [Check("ok", "ledger", f"none yet; the first start creates {config.ledger}")]
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
    return checks


def alert_checks(settings: Settings) -> list[Check]:
    token, chat = settings.telegram_token, settings.telegram_chat_id
    if token and chat:
        telegram = Check("ok", "telegram", "configured; `tbot notify` sends a test message")
    elif token or chat:
        telegram = Check("fail", "telegram", "set both TBOT_TELEGRAM_TOKEN and _CHAT_ID")
    else:
        telegram = Check("warn", "telegram", "not configured: alerts only reach the log")
    heartbeat = (
        Check("ok", "heartbeat", "configured")
        if settings.heartbeat_url
        else Check("warn", "heartbeat", "not configured: nobody hears about a dead bot")
    )
    return [telegram, heartbeat]


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
        return [Check("fail", "api key", str(exc))]
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
        with Ledger(config.ledger) as ledger:
            needed = restore_portfolio(config, ledger).cash
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
    lines.append(f"{failed} problems, {warned} warnings")
    return "\n".join(lines)
