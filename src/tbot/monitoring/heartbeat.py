"""Ping an external monitor so a dead bot gets noticed."""

import asyncio
from collections.abc import Callable

import httpx
import structlog

from tbot.data.http import describe_error

log = structlog.get_logger(__name__)


async def heartbeat_loop(
    url: str,
    interval: float,
    client: httpx.AsyncClient,
    problem: Callable[[], str] = lambda: "",
) -> None:
    """Ping while the bot runs and gets market data; `problem` says why not, if it does not,
    and the monitor then reports the bot down."""
    while True:
        reason = problem()
        if reason:
            log.warning("heartbeat_skipped", reason=reason)
        else:
            try:
                response = await client.get(url, timeout=15.0)
                response.raise_for_status()
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                error = describe_error(exc) if isinstance(exc, httpx.HTTPError) else "invalid URL"
                log.warning("heartbeat_failed", error=error)
        await asyncio.sleep(interval)
