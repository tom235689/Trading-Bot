"""Telegram alerts. Failures are logged, never raised: alerting must not stop trading."""

import asyncio
from typing import Any, Protocol

import httpx
import structlog

from tbot.data.http import describe_error

log = structlog.get_logger(__name__)
API = "https://api.telegram.org"
MAX_LENGTH = 4000  # Telegram allows 4096 characters per message


class Notifier(Protocol):
    async def send(self, text: str) -> bool: ...


class LogNotifier:
    """Fallback when Telegram is not configured."""

    async def send(self, text: str) -> bool:
        log.info("notify", text=text)
        return True


class Telegram:
    def __init__(self, token: str, chat_id: str, client: httpx.AsyncClient) -> None:
        self.base = f"{API}/bot{token}"
        self.url = f"{self.base}/sendMessage"
        self.chat_id = chat_id
        self.client = client

    async def me(self) -> str:
        """The bot's username. Raises httpx errors (401 for a wrong token); never log their URL."""
        response = await self.client.get(f"{self.base}/getMe", timeout=15.0)
        response.raise_for_status()
        return str((response.json().get("result") or {}).get("username", ""))

    async def updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        """Messages sent to the bot (long poll). Raises httpx errors; never log their URL."""
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": '["message"]'}
        if offset is not None:
            params["offset"] = offset
        response = await self.client.get(
            f"{self.base}/getUpdates", params=params, timeout=timeout + 15.0
        )
        response.raise_for_status()
        return list(response.json().get("result", []))

    async def send(self, text: str) -> bool:
        payload = {"chat_id": self.chat_id, "text": text[:MAX_LENGTH]}
        try:
            response = await self.client.post(self.url, json=payload, timeout=15.0)
            response.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            log.warning("telegram_failed", error=describe_error(exc))  # never the URL
            return False


class QueuedNotifier:
    """Sends in order from one background task, so a slow Telegram never holds up an order."""

    def __init__(self, inner: Notifier) -> None:
        self.inner = inner
        self.queue: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, text: str) -> bool:
        self.queue.put_nowait(text)
        return True

    async def run(self) -> None:
        while True:
            text = await self.queue.get()
            try:
                await self.inner.send(text)
            except Exception as exc:  # a notifier must never stop the sender
                log.warning("notify_failed", error=repr(exc))
            finally:
                self.queue.task_done()

    async def flush(self, timeout: float) -> None:
        """Wait until everything queued is sent; run() must be running."""
        try:
            await asyncio.wait_for(self.queue.join(), timeout)
        except TimeoutError:
            log.warning("alerts_not_sent", left=self.queue.qsize())
