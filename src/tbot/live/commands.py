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
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime
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
CONFLICT_ALERT_SECONDS = 86400.0  # the conflict alert goes once a day, not at each clash
REFUSED = frozenset({401, 403, 404})  # a revoked or wrong token: alerts fail the same way
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
Reply = Callable[[str], Awaitable[object]]


def command_of(text: str) -> str:
    """`/Status@my_bot extra` -> `/status`."""
    words = text.strip().split()
    return words[0].split("@")[0].lower() if words else ""


def addressee(text: str) -> str:
    """The bot a command names: `/halt@my_bot 1234` -> `my_bot`, else ""."""
    words = text.strip().split()
    head = words[0] if words else ""
    return head.split("@", 1)[1].lower() if head.startswith("/") and "@" in head else ""


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
            with contextlib.suppress(OSError):  # Windows: another process reads it now
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
    reply: Reply | None = None,
) -> None:
    """Answer the owner's messages sent after `started` (the exchange's time: the PC clock
    may be off); `reply` sends the answers (by default once, straight to Telegram)."""
    offset: int | None = None
    failing = False
    conflict_said = -CONFLICT_ALERT_SECONDS
    chat = Chat(telegram, answer, started, reply or telegram.send, label)
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
                    await chat.handle(update)
                await chat.missed_halts()
            except Exception as exc:  # anything Telegram sends back must never end the session
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else 0
                conflict = status == 409
                if status in REFUSED:  # the heartbeat says so; alerts fail the same way
                    telegram.refuse(f"HTTP {status} when reading messages")
                if not failing:
                    error = describe_error(exc) if isinstance(exc, httpx.HTTPError) else repr(exc)
                    hint = "another process polls this bot token" if conflict else "retrying"
                    log.warning("telegram_commands_failed", error=error, hint=hint)
                # Two programs on one bot clash again at every poll the other wins: the
                # owner hears of it once a day, not at each clash.
                if conflict and time.monotonic() - conflict_said >= CONFLICT_ALERT_SECONDS:
                    conflict_said = time.monotonic()
                    await telegram.send(f"[{label}] {CONFLICT_ALERT}" if label else CONFLICT_ALERT)
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


class Chat:
    """The owner's messages to one session: answered when sent after it started; a /halt
    sent while no session read the chat is told to be sent again, not obeyed."""

    def __init__(
        self, telegram: Telegram, answer: Answer, started: datetime, reply: Reply, label: str
    ) -> None:
        self.telegram = telegram
        self.answer = answer
        self.started = started
        self.reply = reply
        self.label = label
        self.bot = ""  # this bot's username, to tell its commands in a group from others'
        self.missed: list[datetime] = []  # /halt messages from before the start

    async def handle(self, update: object) -> None:
        message = (update.get("message") if isinstance(update, dict) else None) or {}
        chat = message.get("chat") or {}
        if str(chat.get("id", "")) != self.telegram.chat_id:
            return  # someone else
        text = str(message.get("text") or "")
        group = chat.get("type", "private") != "private"
        if group and not self.bot:  # asked once, when a group first writes
            with contextlib.suppress(httpx.HTTPError, ValueError):
                self.bot = (await self.telegram.me()).lower()
        if group:
            named = addressee(text)
            if not text.startswith("/") or (named and self.bot and named != self.bot):
                return  # talk in a group, or a command for another bot
        if message.get("date", 0) < self.started.timestamp():
            if command_of(text) == "/halt":  # obeying it now could surprise; say so
                self.missed.append(datetime.fromtimestamp(message.get("date", 0), UTC))
            return
        try:
            answer = await self.answer(text)
        except Exception as exc:  # a question must never stop the session
            log.exception("telegram_command_failed")
            answer = f"could not answer: {exc!r}"
        if group and self.bot:
            answer = answer.replace("/halt ", f"/halt@{self.bot} ")  # sure to reach this bot
        await self.reply(answer)

    async def missed_halts(self) -> None:
        """Tell of /halt messages that came while no session read the chat."""
        if not self.missed:
            return
        last, count = max(self.missed), len(self.missed)
        self.missed.clear()
        what = "a /halt" if count == 1 else f"{count} /halt messages"
        head = f"[{self.label}] " if self.label else ""
        await self.reply(
            f"{head}{what} (the last sent {last:%H:%M} UTC) came while no session read this "
            "chat, at a restart: not carried out now; send /halt again to halt"
        )
