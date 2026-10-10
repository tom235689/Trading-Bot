from collections.abc import Iterator

import httpx
import pytest

from tbot.data.http import describe_error, get_bytes, retry_after

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


def test_error_description_never_carries_the_url() -> None:
    request = httpx.Request("POST", "https://api.telegram.org/botSECRET/sendMessage")
    response = httpx.Response(401, text='{"ok":false}', request=request)
    status = httpx.HTTPStatusError("boom", request=request, response=response)
    assert describe_error(status) == 'HTTP 401: {"ok":false}'
    assert describe_error(httpx.ConnectError("x", request=request)) == "ConnectError"


def test_a_long_retry_after_fails_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []
    monkeypatch.setattr("tbot.data.http.time.sleep", waits.append)
    client, calls = client_for([httpx.Response(418, headers={"Retry-After": "7200"})])
    with pytest.raises(httpx.HTTPStatusError):
        get_bytes(client, URL)
    assert (calls, waits) == ([1], [])  # banned for two hours: say so now, do not sleep


def test_a_retry_after_date_falls_back_to_the_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []
    monkeypatch.setattr("tbot.data.http.time.sleep", waits.append)
    date = {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
    client, calls = client_for([httpx.Response(429, headers=date), httpx.Response(200)])
    assert get_bytes(client, URL, backoff=2.0) == b""
    assert (calls, waits) == ([2], [2.0])
    limited = httpx.Response(429, headers=date, request=httpx.Request("GET", URL))
    error = httpx.HTTPStatusError("x", request=limited.request, response=limited)
    assert retry_after(error) == 60.0
