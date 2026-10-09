import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from tbot.live.commands import command_loop
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
            task = asyncio.create_task(command_loop(telegram, lambda c: f"answer {c}", started))
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
            task = asyncio.create_task(command_loop(telegram, lambda c: f"answer {c}", started))
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
