"""Ping an external monitor so a dead bot gets noticed."""

import asyncio

import httpx
import structlog

log = structlog.get_logger(__name__)


async def heartbeat_loop(url: str, interval: float, client: httpx.AsyncClient) -> None:
    while True:
        try:
            response = await client.get(url, timeout=15.0)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("heartbeat_failed", error=repr(exc))
        await asyncio.sleep(interval)
