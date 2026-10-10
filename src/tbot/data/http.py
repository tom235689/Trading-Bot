"""HTTP GET with retries for Binance endpoints."""

import time
from collections.abc import Mapping

import httpx

RETRY_STATUS = frozenset({418, 429, 500, 502, 503, 504})
MAX_RETRY_WAIT = 30.0  # seconds; a longer Retry-After (an IP ban) fails at once


def get_bytes(
    client: httpx.Client,
    url: str,
    params: Mapping[str, str | int] | None = None,
    *,
    retries: int = 5,
    backoff: float = 1.0,
) -> bytes | None:
    """GET url. Return None on 404; retry network errors, rate limits, and server errors."""
    for attempt in range(retries):
        last = attempt == retries - 1
        delay = backoff * 2**attempt
        try:
            response = client.get(url, params=params)
        except httpx.TransportError:
            if last:
                raise
            time.sleep(delay)
            continue
        if response.status_code == 404:
            return None
        wait = retry_seconds(response.headers.get("Retry-After"), delay)
        if response.status_code in RETRY_STATUS and not last and wait <= MAX_RETRY_WAIT:
            time.sleep(wait)  # waiting longer would stall the live feed without a word
            continue
        response.raise_for_status()
        return response.content
    raise ValueError("retries must be positive")


def retry_after(exc: BaseException) -> float | None:
    """Seconds a rate-limited client must wait, or None if this is no rate limit."""
    if not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code not in (418, 429):
        return None
    return retry_seconds(exc.response.headers.get("Retry-After"), 60.0)


def retry_seconds(value: str | None, default: float) -> float:
    """A Retry-After in seconds; the HTTP-date form, which Binance does not send, or junk
    falls back to default."""
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default


def describe_error(exc: httpx.HTTPError) -> str:
    """Error text without the request URL, which may carry a token."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
    return type(exc).__name__
