"""Read-only Telegram commands: the owner asks, the session answers. Nothing here trades.

Only messages from the configured chat, sent after the session started, are answered.
One process per bot token may poll for messages; Telegram refuses a second one (409).
"""

import asyncio
from collections.abc import Callable
from datetime import datetime

import httpx
import structlog

from tbot.data.http import describe_error
from tbot.monitoring.telegram import Telegram

log = structlog.get_logger(__name__)
POLL_SECONDS = 50  # long poll: Telegram holds the request until a message arrives
RETRY_SECONDS = 60
CONFLICT_SECONDS = 600  # another process polls this bot; look again later
HELP = (
    "/status: equity, drawdown, positions, kill switch\n"
    "/fills: the last fills\n"
    "/help: this list\n"
    "Read only: nothing here trades or stops the bot."
)

Answer = Callable[[str], str]


def command_of(text: str) -> str:
    """`/Status@my_bot extra` -> `/status`."""
    words = text.strip().split()
    return words[0].split("@")[0].lower() if words else ""


async def command_loop(telegram: Telegram, answer: Answer, started: datetime) -> None:
    offset: int | None = None
    failing = False
    while True:
        try:
            updates = await telegram.updates(offset, POLL_SECONDS)
            for update in updates:
                offset = int(update["update_id"]) + 1
                await _handle(telegram, answer, started, update)
        except Exception as exc:  # anything Telegram sends back must never end the session
            conflict = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 409
            if not failing:
                error = describe_error(exc) if isinstance(exc, httpx.HTTPError) else repr(exc)
                hint = "another process polls this bot token" if conflict else "retrying"
                log.warning("telegram_commands_failed", error=error, hint=hint)
            failing = True
            await asyncio.sleep(CONFLICT_SECONDS if conflict else RETRY_SECONDS)
            continue
        failing = False


async def _handle(telegram: Telegram, answer: Answer, started: datetime, update: object) -> None:
    message = (update.get("message") if isinstance(update, dict) else None) or {}
    chat = str((message.get("chat") or {}).get("id", ""))
    if chat != telegram.chat_id or message.get("date", 0) < started.timestamp():
        return  # someone else, or a message from before this session
    try:
        reply = answer(command_of(str(message.get("text") or "")))
    except Exception as exc:  # a question must never stop the session
        log.exception("telegram_command_failed")
        reply = f"could not answer: {exc!r}"
    await telegram.send(reply)
