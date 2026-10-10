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
# A REST bar counts as closed only this long after its close: the corrected clock can
# still run up to half a round trip ahead, or a whole Windows time step until the resync.
CLOSE_GRACE = timedelta(seconds=5)

log = structlog.get_logger(__name__)


class ServerClock:
    """Server time from the last sync onward, on the monotonic clock: a step of the local
    wall clock (Windows time sync) moves nothing until the next sync measures it."""

    def __init__(self, client: httpx.Client) -> None:
        self.client = client
        self.offset = timedelta(0)  # server minus local wall clock, as last measured
        self.synced = False
        self.anchor: tuple[float, float] | None = None  # server time at a monotonic time

    def sync(self) -> float:
        """Measure the offset with one quick round trip; return it in seconds.

        Each attempt is timed on its own, so retry backoff never counts as latency. When
        every round trip is slow, a clock synced before keeps its offset.
        """
        # Round trip, server time, and the monotonic and wall clocks at the midpoint.
        best: tuple[float, float, float, float] | None = None
        for attempt in range(ATTEMPTS):
            before, wall_before = time.monotonic(), time.time()
            try:
                body = get_bytes(self.client, SERVER_TIME_URL, retries=1)
            except httpx.HTTPError:
                if attempt == ATTEMPTS - 1 and best is None:
                    raise
                time.sleep(2**attempt)
                continue
            after, wall_after = time.monotonic(), time.time()
            if body is None:
                raise LookupError(f"not found: {SERVER_TIME_URL}")
            server = json.loads(body)["serverTime"] / 1000
            sample = (after - before, server, (before + after) / 2, (wall_before + wall_after) / 2)
            if best is None or sample[0] < best[0]:
                best = sample
            if sample[0] <= MAX_ROUND_TRIP:
                break
        assert best is not None
        if best[0] > MAX_ROUND_TRIP and self.synced:
            log.warning("clock_sync_slow", round_trip=round(best[0], 3), hint="offset kept")
            return self.offset.total_seconds()
        self.anchor = (best[1], best[2])
        self.offset = timedelta(seconds=best[1] - best[3])
        self.synced = True
        seconds = self.offset.total_seconds()
        level = log.warning if abs(seconds) > WARN_OFFSET else log.info
        level("clock_synced", offset_seconds=round(seconds, 3))
        return seconds

    def now(self) -> datetime:
        if self.anchor is None:
            return datetime.now(UTC)
        server, at = self.anchor
        return datetime.fromtimestamp(server + time.monotonic() - at, UTC)
