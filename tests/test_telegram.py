import asyncio

import httpx

from tbot.monitoring.telegram import LogNotifier, Telegram


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
