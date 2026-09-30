"""Telegram alerts. Failures are logged, never raised: alerting must not stop trading."""

from typing import Protocol

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
        self.url = f"{API}/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.client = client

    async def send(self, text: str) -> bool:
        payload = {"chat_id": self.chat_id, "text": text[:MAX_LENGTH]}
        try:
            response = await self.client.post(self.url, json=payload, timeout=15.0)
            response.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            log.warning("telegram_failed", error=describe_error(exc))  # never the URL
            return False
