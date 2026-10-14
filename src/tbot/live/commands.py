"""Telegram commands: the owner asks, the session answers. Only /halt trades: it sells and
stops trading, once a second message with a code confirms it.

Only messages from the configured chat, sent after the session started, are answered.
One process per bot token may poll for messages (Telegram refuses a second one, 409), so
the sessions of a clone take turns (PollTurn).
"""

import asyncio
import contextlib
import hashlib
import time
from collections.abc import Callable, Coroutine
from datetime import datetime
from pathlib import Path
from typing import IO, Any

import httpx
import structlog

from tbot.core.filelock import lock_file
from tbot.data.http import describe_error
from tbot.monitoring.telegram import Telegram

log = structlog.get_logger(__name__)
POLL_SECONDS = 50  # long poll: Telegram holds the request until a message arrives
RETRY_SECONDS = 60
CONFLICT_SECONDS = 600  # another process polls this bot; look again later
TURN_DIR = Path("data")  # every tbot process of a clone runs in its repository
TURN_SECONDS = 10.0  # how often a process waiting for its turn looks again
WANT_FRESH = 60.0  # a request for the turn this old was left by a process that ended
RANK_SESSION, RANK_REAL_MONEY, RANK_PAIRING = 0, 1, 2  # who reads the messages first
CONFLICT_ALERT = (
    "Telegram commands paused: another program reads this bot's messages (a second clone "
    "or computer with the same bot, or a webhook), so /halt may not reach this session. "
    "Give each clone its own bot: `tbot notify` with a new token"
)
HELP = (
    "/status: equity, drawdown, positions, kill switch\n"
    "/fills: the last fills\n"
    "/halt: sell everything the bot holds and stop its trading (asks to confirm)\n"
    "/help: this list\n"
    "Nothing else here trades, and only `tbot resume` on the computer trades again."
)

Answer = Callable[[str], Coroutine[Any, Any, str]]  # the message's text -> the reply


def command_of(text: str) -> str:
    """`/Status@my_bot extra` -> `/status`."""
    words = text.strip().split()
    return words[0].split("@")[0].lower() if words else ""


class PollTurn:
    """Whose turn it is to read a bot token's messages, among the tbot processes of a clone.

    A file lock decides; a higher rank asks the holder to let go: a real-money session
    before the others, so /halt reaches it, and `tbot notify` before every session while it
    pairs a chat.
    """

    def __init__(self, token: str, rank: int) -> None:
        name = f"telegram-{hashlib.sha256(token.encode()).hexdigest()[:12]}"
        self.path = TURN_DIR / f"{name}.lock"
        self.want = TURN_DIR / f"{name}.want"
        self.rank = rank
        self.handle: IO[str] | None = None
        self.alone = False  # no place for the lock: poll as before turns existed

    @property
    def holding(self) -> bool:
        return self.handle is not None

    def waiting_rank(self) -> int:
        """The rank of a process waiting for the turn, or -1."""
        try:
            fresh = time.time() - self.want.stat().st_mtime < WANT_FRESH
            return int(self.want.read_text(encoding="utf-8").strip()) if fresh else -1
        except (OSError, ValueError):
            return -1

    def wanted(self) -> bool:
        """A process of a higher rank waits for the turn."""
        return self.waiting_rank() > self.rank

    def take(self) -> bool:
        """Whether this process may poll now; it keeps the turn until it releases it."""
        if self.handle is not None or self.alone:
            return True
        if self.wanted():
            return False  # a higher rank goes first
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+")
        except OSError as exc:
            log.warning("telegram_turn_unavailable", error=repr(exc), path=str(self.path))
            self.alone = True
            return True
        try:
            lock_file(handle, lock=True)
        except OSError:
            handle.close()
            if self.rank > RANK_SESSION and self.waiting_rank() <= self.rank:
                with contextlib.suppress(OSError):  # ask the holder to let go
                    self.want.write_text(str(self.rank), encoding="utf-8")
            return False
        self.handle = handle
        if self.waiting_rank() == self.rank:
            self.want.unlink(missing_ok=True)
        return True

    def release(self) -> None:
        if self.handle is not None:
            with contextlib.suppress(OSError):
                lock_file(self.handle, lock=False)
            self.handle.close()
            self.handle = None


async def command_loop(
    telegram: Telegram,
    answer: Answer,
    started: datetime,
    turn: PollTurn | None = None,
    label: str = "",
) -> None:
    offset: int | None = None
    failing = False
    try:
        while True:
            if turn is not None:
                if turn.holding and turn.wanted():
                    await _confirm(telegram, offset)  # or the next holder answers them again
                    turn.release()
                    log.info("telegram_turn_given", hint="a process of higher rank reads them")
                if not turn.take():
                    await asyncio.sleep(TURN_SECONDS)
                    continue
            try:
                updates = await telegram.updates(offset, POLL_SECONDS)
                for update in updates:
                    offset = int(update["update_id"]) + 1
                    await _handle(telegram, answer, started, update)
            except Exception as exc:  # anything Telegram sends back must never end the session
                conflict = (
                    isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 409
                )
                if not failing:
                    error = describe_error(exc) if isinstance(exc, httpx.HTTPError) else repr(exc)
                    hint = "another process polls this bot token" if conflict else "retrying"
                    log.warning("telegram_commands_failed", error=error, hint=hint)
                    if conflict:  # outside this clone's turns: the owner must hear of it
                        await telegram.send(
                            f"[{label}] {CONFLICT_ALERT}" if label else CONFLICT_ALERT
                        )
                failing = True
                if turn is not None and (conflict or turn.wanted()):
                    turn.release()  # nobody waits through this sleep for a turn not used
                await asyncio.sleep(CONFLICT_SECONDS if conflict else RETRY_SECONDS)
                continue
            failing = False
    finally:
        if turn is not None:
            turn.release()


async def _confirm(telegram: Telegram, offset: int | None) -> None:
    """Tell Telegram the updates read so far are done with."""
    if offset is not None:
        with contextlib.suppress(httpx.HTTPError):
            await telegram.updates(offset, 0)


async def _handle(telegram: Telegram, answer: Answer, started: datetime, update: object) -> None:
    message = (update.get("message") if isinstance(update, dict) else None) or {}
    chat = str((message.get("chat") or {}).get("id", ""))
    if chat != telegram.chat_id or message.get("date", 0) < started.timestamp():
        return  # someone else, or a message from before this session
    try:
        reply = await answer(str(message.get("text") or ""))
    except Exception as exc:  # a question must never stop the session
        log.exception("telegram_command_failed")
        reply = f"could not answer: {exc!r}"
    await telegram.send(reply)
