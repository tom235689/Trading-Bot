"""HTTP GET with retries for Binance endpoints."""

import time
from collections.abc import Mapping

import httpx

RETRY_STATUS = frozenset({418, 429, 500, 502, 503, 504})


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
        if response.status_code in RETRY_STATUS and not last:
            time.sleep(float(response.headers.get("Retry-After", delay)))
            continue
        response.raise_for_status()
        return response.content
    raise ValueError("retries must be positive")
