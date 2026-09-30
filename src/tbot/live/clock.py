"""Clock corrected to Binance server time. Local clocks drift by seconds; bars do not."""

import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import structlog

from tbot.data.http import get_bytes

SERVER_TIME_URL = "https://data-api.binance.vision/api/v3/time"
WARN_OFFSET = 1.0  # seconds
ATTEMPTS = 4
MAX_ROUND_TRIP = 2.0  # seconds; a slower sample says little about the offset

log = structlog.get_logger(__name__)


class ServerClock:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client
        self.offset = timedelta(0)  # server minus local

    def sync(self) -> float:
        """Measure the offset with one quick round trip; return it in seconds.

        Each attempt is timed on its own, so retry backoff never counts as latency.
        """
        for attempt in range(ATTEMPTS):
            before = time.time()
            try:
                body = get_bytes(self.client, SERVER_TIME_URL, retries=1)
            except httpx.HTTPError:
                if attempt == ATTEMPTS - 1:
                    raise
                time.sleep(2**attempt)
                continue
            after = time.time()
            if after - before <= MAX_ROUND_TRIP or attempt == ATTEMPTS - 1:
                break
        if body is None:
            raise LookupError(f"not found: {SERVER_TIME_URL}")
        server = json.loads(body)["serverTime"] / 1000
        self.offset = timedelta(seconds=server - (before + after) / 2)
        seconds = self.offset.total_seconds()
        level = log.warning if abs(seconds) > WARN_OFFSET else log.info
        level("clock_synced", offset_seconds=round(seconds, 3))
        return seconds

    def now(self) -> datetime:
        return datetime.now(UTC) + self.offset
