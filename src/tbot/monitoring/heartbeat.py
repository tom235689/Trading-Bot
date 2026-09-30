"""Ping an external monitor so a dead bot gets noticed."""

import asyncio

import httpx
import structlog

from tbot.data.http import describe_error

log = structlog.get_logger(__name__)


async def heartbeat_loop(url: str, interval: float, client: httpx.AsyncClient) -> None:
    while True:
        try:
            response = await client.get(url, timeout=15.0)
            response.raise_for_status()
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            error = describe_error(exc) if isinstance(exc, httpx.HTTPError) else "invalid URL"
            log.warning("heartbeat_failed", error=error)
        await asyncio.sleep(interval)
