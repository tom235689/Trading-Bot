from collections.abc import Iterator

import httpx
import pytest

from tbot.data.http import get_bytes

URL = "https://example.test/x"


def client_for(responses: list[httpx.Response | Exception]) -> tuple[httpx.Client, list[int]]:
    """Client that replays responses in order and counts calls."""
    calls = [0]
    replay: Iterator[httpx.Response | Exception] = iter(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        item = next(replay)
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.Client(transport=httpx.MockTransport(handle)), calls


def test_returns_content() -> None:
    client, calls = client_for([httpx.Response(200, content=b"ok")])
    assert get_bytes(client, URL) == b"ok"
    assert calls == [1]


def test_not_found_returns_none() -> None:
    client, _ = client_for([httpx.Response(404)])
    assert get_bytes(client, URL) is None


def test_retries_server_errors_and_network_errors() -> None:
    client, calls = client_for(
        [
            httpx.Response(503),
            httpx.ConnectError("down"),
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, content=b"ok"),
        ]
    )
    assert get_bytes(client, URL, backoff=0) == b"ok"
    assert calls == [4]


def test_gives_up_after_retries() -> None:
    client, calls = client_for([httpx.Response(500)] * 3)
    with pytest.raises(httpx.HTTPStatusError):
        get_bytes(client, URL, retries=3, backoff=0)
    assert calls == [3]


def test_client_error_is_not_retried() -> None:
    client, calls = client_for([httpx.Response(400)])
    with pytest.raises(httpx.HTTPStatusError):
        get_bytes(client, URL, backoff=0)
    assert calls == [1]
