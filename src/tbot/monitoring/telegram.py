"""Telegram alerts. Failures are logged, never raised: alerting must not stop trading."""

import asyncio
import time
from typing import Any, Protocol, runtime_checkable

import httpx
import structlog

from tbot.data.http import describe_error

log = structlog.get_logger(__name__)
API = "https://api.telegram.org"
MAX_LENGTH = 4000  # Telegram allows 4096 characters per message
RETRY_FIRST = 5.0  # seconds before an alert Telegram did not take is sent again
RETRY_MAX = 300.0
MAX_AGE = 86400.0  # an alert still not sent after a day is dropped


class Notifier(Protocol):
    async def send(self, text: str) -> bool: ...


@runtime_checkable
class Retrying(Protocol):
    async def deliver(self, text: str) -> float | None:
        """Send once: None when done, else at least how many seconds to wait before a retry."""


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
        body = response.json()
        result = body.get("result") if isinstance(body, dict) else None
        return str(result.get("username", "")) if isinstance(result, dict) else ""

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
        return await self.deliver(text) is None

    async def deliver(self, text: str) -> float | None:
        """Send once. None when done: sent, or refused in a way a retry cannot change;
        else the seconds Telegram asked to wait (0 when it did not say)."""
        payload = {"chat_id": self.chat_id, "text": text[:MAX_LENGTH]}
        try:
            response = await self.client.post(self.url, json=payload, timeout=15.0)
        except httpx.HTTPError as exc:  # no network, a timeout: try again
            log.warning("telegram_failed", error=describe_error(exc))  # never the URL
            return 0.0
        if response.is_success:
            return None
        log.warning("telegram_failed", error=f"HTTP {response.status_code}: {response.text[:200]}")
        if response.status_code == 429:
            return _retry_after(response)
        return 0.0 if response.status_code >= 500 else None


class QueuedNotifier:
    """Sends in order from one background task, so a slow Telegram never holds up an order."""

    def __init__(self, inner: Notifier) -> None:
        self.inner = inner
        self.queue: asyncio.Queue[tuple[str, float]] = asyncio.Queue()

    async def send(self, text: str) -> bool:
        self.queue.put_nowait((text, time.monotonic()))
        return True

    async def run(self) -> None:
        while True:
            text, queued = await self.queue.get()
            try:
                await self._deliver(text, queued)
            except Exception as exc:  # a notifier must never stop the sender
                log.warning("notify_failed", error=repr(exc))
            finally:
                self.queue.task_done()

    async def _deliver(self, text: str, queued: float) -> None:
        """Try until it is sent: an alert raised in an outage is the one that matters."""
        if not isinstance(self.inner, Retrying):
            await self.inner.send(text)
            return
        backoff = RETRY_FIRST
        while (wait := await self.inner.deliver(text)) is not None:
            if time.monotonic() - queued > MAX_AGE:
                log.warning("alert_dropped", reason="not sent for a day")
                return
            await asyncio.sleep(max(wait, backoff))
            backoff = min(backoff * 2, RETRY_MAX)

    async def flush(self, timeout: float) -> None:
        """Wait until everything queued is sent; run() must be running."""
        try:
            await asyncio.wait_for(self.queue.join(), timeout)
        except TimeoutError:
            log.warning("alerts_not_sent", left=self.queue.qsize())


def _retry_after(response: httpx.Response) -> float:
    try:
        return float(response.json()["parameters"]["retry_after"])
    except (ValueError, KeyError, TypeError):
        return 0.0
