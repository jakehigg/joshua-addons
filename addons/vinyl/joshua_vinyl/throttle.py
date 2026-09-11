"""A minimum interval between requests to one service.

Discogs allows 60 requests each minute and MusicBrainz about one each
second. Both refuse a client that goes faster, so every request waits here
first. The clock and the sleep are injectable, so a test never waits.
"""

from __future__ import annotations

import time
from collections.abc import Callable


class Throttle:
    """Wait so that two calls are at least ``min_interval`` seconds apart."""

    def __init__(
        self,
        min_interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> float:
        """Block until the interval has passed. Return the seconds slept."""
        now = self._clock()
        slept = 0.0
        if self._last is not None:
            remaining = self.min_interval - (now - self._last)
            if remaining > 0:
                self._sleep(remaining)
                slept = remaining
                now = self._clock()
        self._last = now
        return slept
