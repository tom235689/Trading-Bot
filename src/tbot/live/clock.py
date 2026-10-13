"""Clock corrected to Binance server time. Local clocks drift by seconds; bars do not."""

import json
import sys
import time
from datetime import UTC, datetime, timedelta

import httpx
import structlog

from tbot.data.http import get_bytes

SERVER_TIME_URL = "https://data-api.binance.vision/api/v3/time"
WARN_OFFSET = 3.0  # seconds; the bot corrects any offset, but a large one needs a fix
ATTEMPTS = 4
MAX_ROUND_TRIP = 2.0  # seconds; a slower sample says little about the offset
# A REST bar counts as closed only this long after its close: the corrected clock can
# still run up to half a round trip ahead, or a whole Windows time step until the resync.
CLOSE_GRACE = timedelta(seconds=5)

log = structlog.get_logger(__name__)

if sys.platform == "linux":

    def _ticks() -> float:
        """Monotonic time that also counts a suspend (Linux's monotonic clock stops)."""
        return time.clock_gettime(time.CLOCK_BOOTTIME)

else:

    def _ticks() -> float:
        return time.monotonic()  # Windows and macOS count sleep already


class ServerClock:
    """Server time from the last sync onward, on the monotonic clock: a step of the local
    wall clock (Windows time sync) moves nothing until the next sync measures it."""

    def __init__(self, client: httpx.Client, warn_offset: float = WARN_OFFSET) -> None:
        self.client = client
        self.warn_offset = warn_offset  # a larger offset is logged as a warning
        self.offset = timedelta(0)  # server minus local wall clock, as last measured
        self.synced = False
        self.anchor: tuple[float, float] | None = None  # server time at a monotonic time
        self.reported: float | None = None  # the offset last logged as a warning

    def sync(self) -> float:
        """Measure the offset with one quick round trip; return it in seconds.

        Each attempt is timed on its own, so retry backoff never counts as latency. When
        every round trip is slow, a clock synced before keeps its offset.
        """
        # Round trip, server time, and the monotonic and wall clocks at the midpoint.
        best: tuple[float, float, float, float] | None = None
        for attempt in range(ATTEMPTS):
            before, wall_before = _ticks(), time.time()
            try:
                body = get_bytes(self.client, SERVER_TIME_URL, retries=1)
            except httpx.HTTPError:
                if attempt == ATTEMPTS - 1 and best is None:
                    raise
                time.sleep(2**attempt)
                continue
            after, wall_after = _ticks(), time.time()
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
        # Warned once, and again when it moves: every resync would repeat it otherwise.
        large = abs(seconds) > self.warn_offset
        moved = self.reported is None or abs(seconds - self.reported) > 1.0
        if large and moved:
            self.reported = seconds
        level = log.warning if large and moved else log.info
        level("clock_synced", offset_seconds=round(seconds, 3))
        return seconds

    def now(self) -> datetime:
        if self.anchor is None:
            return datetime.now(UTC)
        server, at = self.anchor
        return datetime.fromtimestamp(server + _ticks() - at, UTC)
