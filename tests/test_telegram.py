import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from tbot.live.commands import command_loop, command_of
from tbot.live.runner import heartbeat_problem
from tbot.monitoring.heartbeat import heartbeat_loop
from tbot.monitoring.telegram import LogNotifier, QueuedNotifier, Telegram


def test_send_posts_to_bot_api() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    async def run() -> bool:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await Telegram("token", "42", client).send("hello")

    assert asyncio.run(run()) is True
    assert seen[0].url.path == "/bottoken/sendMessage"
    assert seen[0].content == b'{"chat_id":"42","text":"hello"}'


def test_send_failure_is_swallowed() -> None:
    async def run() -> bool:
        transport = httpx.MockTransport(lambda request: httpx.Response(500))
        async with httpx.AsyncClient(transport=transport) as client:
            return await Telegram("token", "42", client).send("hello")

    assert asyncio.run(run()) is False


def test_log_notifier() -> None:
    assert asyncio.run(LogNotifier().send("x")) is True


def test_queued_alerts_keep_their_order_and_never_wait() -> None:
    class Slow:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, text: str) -> bool:
            await asyncio.sleep(0.05)
            self.sent.append(text)
            return True

    async def run() -> tuple[float, list[str]]:
        slow = Slow()
        queued = QueuedNotifier(slow)
        sender = asyncio.create_task(queued.run())
        loop = asyncio.get_running_loop()
        began = loop.time()
        for text in ("a", "b", "c"):
            assert await queued.send(text)
        waited = loop.time() - began
        await queued.flush(5.0)
        sender.cancel()
        return waited, slow.sent

    waited, sent = asyncio.run(run())
    assert waited < 0.01  # an order is never held up by Telegram
    assert sent == ["a", "b", "c"]


async def echo(text: str) -> str:
    return f"answer {command_of(text)}"


def test_commands_are_answered_only_for_the_owner() -> None:
    started = datetime(2024, 1, 1, tzinfo=UTC)
    stamp = int(started.timestamp())
    offsets: list[str | None] = []
    sent: list[str] = []
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 42}, "date": stamp + 9, "text": "/Status@bot"}},
        {"update_id": 2, "message": {"chat": {"id": 7}, "date": stamp + 9, "text": "/status"}},
        {"update_id": 3, "message": {"chat": {"id": 42}, "date": stamp - 9, "text": "/fills"}},
        {"update_id": 4, "message": {"chat": {"id": 42}, "date": stamp + 9, "text": "hello"}},
    ]

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            offsets.append(request.url.params.get("offset"))
            if len(offsets) > 1:
                await asyncio.sleep(10)  # a long poll with nothing new
            return httpx.Response(200, json={"ok": True, "result": updates})
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            telegram = Telegram("token", "42", client)
            task = asyncio.create_task(command_loop(telegram, echo, started))
            async with asyncio.timeout(5):  # a regression fails instead of hanging
                while len(sent) < 2 or len(offsets) < 2:
                    await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert sent == ["answer /status", "answer hello"]  # not the stranger, not the old message
    assert offsets == [None, "5"]  # every update is confirmed


def test_an_odd_reply_from_telegram_never_ends_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = datetime(2024, 1, 1, tzinfo=UTC)
    polls: list[int] = []
    sent: list[str] = []
    replies = [
        httpx.Response(200, text="<html>blocked</html>"),  # a proxy page
        httpx.Response(200, json=["not", "a", "dict"]),
        httpx.Response(200, json={"ok": True, "result": [{"no": "update_id"}]}),
        httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 5,
                        "message": {
                            "chat": {"id": 42},
                            "date": int(started.timestamp()) + 9,
                            "text": "/status",
                        },
                    }
                ],
            },
        ),
    ]

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            polls.append(1)
            if len(polls) > len(replies):
                await asyncio.sleep(10)
            return replies[min(len(polls), len(replies)) - 1]
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("tbot.live.commands.RETRY_SECONDS", 0)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            telegram = Telegram("token", "42", client)
            task = asyncio.create_task(command_loop(telegram, echo, started))
            async with asyncio.timeout(5):
                while not sent:
                    await asyncio.sleep(0.01)
            assert not task.done()  # still polling after three bad replies
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert sent == ["answer /status"]


def test_me_reads_the_bot_name_and_survives_an_odd_answer() -> None:
    answers = iter([{"ok": True, "result": {"username": "tbot_bot"}}, ["not", "a", "dict"]])

    async def scenario() -> list[str]:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=next(answers)))
        async with httpx.AsyncClient(transport=transport) as client:
            bot = Telegram("123:abc", "", client)
            return [await bot.me(), await bot.me()]

    assert asyncio.run(scenario()) == ["tbot_bot", ""]


def send_through_queue(answers: list[httpx.Response | Exception]) -> int:
    """Send one alert through the queue; return how many tries it took."""
    tries = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal tries
        tries += 1
        answer = answers.pop(0) if answers else httpx.Response(200, json={"ok": True})
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            queued = QueuedNotifier(Telegram("token", "42", client))
            sender = asyncio.create_task(queued.run())
            await queued.send("stale stream BTCUSDT 4h")
            await queued.flush(5.0)
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    asyncio.run(run())
    return tries


@pytest.fixture
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tbot.monitoring.telegram.RETRY_FIRST", 0.0)


@pytest.mark.usefixtures("fast_retries")
def test_an_alert_is_sent_once_telegram_can_be_reached_again() -> None:
    down: list[httpx.Response | Exception] = [
        httpx.ConnectError("down"),
        httpx.Response(502),
        httpx.ConnectError("down"),
    ]
    assert send_through_queue(down) == 4


@pytest.mark.usefixtures("fast_retries")
def test_telegram_rate_limits_are_waited_out_and_refusals_dropped() -> None:
    busy = {"ok": False, "parameters": {"retry_after": 0.01}}
    assert send_through_queue([httpx.Response(429, json=busy)]) == 2
    assert send_through_queue([httpx.Response(400, json={"ok": False})]) == 1  # chat not found


@pytest.mark.usefixtures("fast_retries")
def test_an_alert_not_sent_for_a_day_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tbot.monitoring.telegram.MAX_AGE", -1.0)
    assert send_through_queue([httpx.ConnectError("down")] * 5) == 1


def test_the_heartbeat_stops_while_the_bot_cannot_trade() -> None:
    pings = 0
    problems = ["stale streams: BTCUSDT 4h"]

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal pings
        pings += 1
        return httpx.Response(200)

    async def run() -> int:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            beat = asyncio.create_task(
                heartbeat_loop(
                    "https://monitor.example/ping", 0.01, client, lambda: "".join(problems)
                )
            )
            await asyncio.sleep(0.1)
            skipped = pings
            problems.clear()  # the stream is back
            await asyncio.sleep(0.1)
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)
            return skipped

    assert asyncio.run(run()) == 0  # no ping while it cannot trade: the monitor reports it
    assert pings > 0


def test_a_refused_message_is_not_reported_as_sent() -> None:
    async def run() -> tuple[bool, float | None]:
        refuse = httpx.MockTransport(lambda request: httpx.Response(400, json={"ok": False}))
        async with httpx.AsyncClient(transport=refuse) as client:
            bot = Telegram("token", "42", client)
            return await bot.send("x"), await bot.deliver("x")

    assert asyncio.run(run()) == (False, None)  # `tbot notify` must not say it was sent


def test_alerts_follow_a_group_that_became_a_supergroup() -> None:
    chats: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        chat = json.loads(request.content)["chat_id"]
        chats.append(chat)
        if chat == "-4001":
            moved = {"migrate_to_chat_id": -1009876}
            return httpx.Response(400, json={"ok": False, "parameters": moved})
        return httpx.Response(200, json={"ok": True})

    async def run() -> tuple[bool, bool, str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            bot = Telegram("token", "-4001", client)
            return await bot.send("one"), await bot.send("two"), bot.chat_id

    assert asyncio.run(run()) == (True, True, "-1009876")
    assert chats == ["-4001", "-1009876", "-1009876"]


def test_a_second_reader_of_the_bot_is_told_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tbot.live.commands.CONFLICT_SECONDS", 0.01)
    sent: list[str] = []
    polls: list[int] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            polls.append(1)
            return httpx.Response(409, json={"ok": False, "description": "Conflict"})
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True})

    async def echo(text: str) -> str:
        return text

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            telegram = Telegram("token", "42", client)
            started = datetime(2024, 1, 1, tzinfo=UTC)
            task = asyncio.create_task(command_loop(telegram, echo, started, label="live"))
            async with asyncio.timeout(5):
                while len(polls) < 5:
                    await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert len(sent) == 1  # once, not at every try
    assert sent[0].startswith("[live] Telegram commands paused: another program reads this bot")


def test_two_readers_that_take_turns_winning_are_told_once_a_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tbot.live.commands.CONFLICT_SECONDS", 0.01)
    sent: list[str] = []
    polls: list[int] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            polls.append(1)
            if len(polls) % 2:  # the other program cut this poll short
                return httpx.Response(409, json={"ok": False, "description": "Conflict"})
            return httpx.Response(200, json={"ok": True, "result": []})
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            started = datetime(2024, 1, 1, tzinfo=UTC)
            task = asyncio.create_task(command_loop(Telegram("t", "42", client), echo, started))
            async with asyncio.timeout(5):
                while len(polls) < 8:
                    await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert len(sent) == 1  # not after every poll the other program won


def test_a_halt_sent_while_no_session_read_the_chat_is_told_not_obeyed() -> None:
    started = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    stamp = int(started.timestamp())
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 42}, "date": stamp - 90, "text": "/halt"}},
        {"update_id": 2, "message": {"chat": {"id": 42}, "date": stamp - 30, "text": "/halt 12"}},
        {"update_id": 3, "message": {"chat": {"id": 42}, "date": stamp - 20, "text": "/fills"}},
    ]
    polls: list[int] = []
    replies: list[str] = []
    asked: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        polls.append(1)
        if len(polls) > 1:
            await asyncio.sleep(10)
        return httpx.Response(200, json={"ok": True, "result": updates})

    async def answer(text: str) -> str:
        asked.append(text)
        return text

    async def reply(text: str) -> bool:
        replies.append(text)
        return True

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            telegram = Telegram("t", "42", client)
            loop = command_loop(telegram, answer, started, label="live", reply=reply)
            task = asyncio.create_task(loop)
            async with asyncio.timeout(5):
                while not replies:
                    await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert asked == []  # nothing from before the start is carried out
    assert replies == [
        "[live] 2 /halt messages (the last sent 11:59 UTC) came while no session read this "
        "chat, at a restart: not carried out now; send /halt again to halt"
    ]


def test_in_a_group_only_commands_for_this_bot_are_answered() -> None:
    started = datetime(2024, 1, 1, tzinfo=UTC)
    stamp = int(started.timestamp()) + 9
    group = {"id": 42, "type": "supergroup"}
    texts = ["good morning", "/status@other_bot", "/status@TBot_Bot", "/halt"]
    updates = [
        {"update_id": i, "message": {"chat": group, "date": stamp, "text": text}}
        for i, text in enumerate(texts, 1)
    ]
    polls: list[int] = []
    sent: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "TBot_Bot"}})
        if request.url.path.endswith("/getUpdates"):
            polls.append(1)
            if len(polls) > 1:
                await asyncio.sleep(10)
            return httpx.Response(200, json={"ok": True, "result": updates})
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True})

    async def answer(text: str) -> str:
        return "send /halt 1234 to go on" if command_of(text) == "/halt" else text

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            task = asyncio.create_task(command_loop(Telegram("t", "42", client), answer, started))
            async with asyncio.timeout(5):
                while len(sent) < 2:
                    await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert sent == ["/status@TBot_Bot", "send /halt@tbot_bot 1234 to go on"]


def test_a_token_telegram_refuses_when_reading_stops_the_heartbeat() -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sendChatAction"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    async def run() -> tuple[str, str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            telegram = Telegram("t", "42", client)
            started = datetime(2024, 1, 1, tzinfo=UTC)
            task = asyncio.create_task(command_loop(telegram, echo, started))
            async with asyncio.timeout(5):
                while not telegram.refused:
                    await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            refused = telegram.refused
            await telegram.probe()  # fixed meanwhile: alerts get through again
            return refused, telegram.refused

    refused, after = asyncio.run(run())
    assert refused == "HTTP 401 when reading messages"
    assert heartbeat_problem([], "", None, refused).startswith("Telegram refuses alerts")
    assert after == ""


def test_alerts_that_do_not_get_through_stop_the_heartbeat() -> None:
    class Down:
        async def deliver(self, text: str) -> float | None:
            return 0.0  # no network: tried again

        async def send(self, text: str) -> bool:
            return False

    async def run() -> float:
        queued = QueuedNotifier(Down())
        sender = asyncio.create_task(queued.run())
        await queued.send("KILL SWITCH")
        await asyncio.sleep(0.05)
        waited = queued.undelivered()
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)
        return waited

    assert asyncio.run(run()) > 0
    assert heartbeat_problem([], "", None, "", 960.0) == "alerts not delivered for 16 minutes"
    assert heartbeat_problem([], "", None, "", 60.0) == ""
