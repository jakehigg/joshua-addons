"""The task clock of the worker: a work budget that pauses while ``ask`` waits.

The clock measures work. Time spent waiting for an answer is not work, so
``pause`` stops the clock and ``resume`` starts it again. The wait has its
own limit, ``ask_wait_s``, which the ``Asker`` enforces.
"""

from __future__ import annotations

import time
from collections.abc import Callable


class Clock:
    """A budget of seconds of work, from ``start`` (a ``now()`` value)."""

    def __init__(
        self,
        budget_s: float,
        *,
        start: float | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.budget_s = budget_s
        self._now = now
        self.start = now() if start is None else start
        self.paused_s = 0.0
        self._paused_at: float | None = None

    @property
    def paused(self) -> bool:
        return self._paused_at is not None

    def pause(self) -> None:
        """Stop the clock. A second pause changes nothing."""
        if self._paused_at is None:
            self._paused_at = self._now()

    def resume(self) -> None:
        """Start the clock again. A resume without a pause changes nothing."""
        if self._paused_at is not None:
            self.paused_s += self._now() - self._paused_at
            self._paused_at = None

    def elapsed(self) -> float:
        """The seconds of work since the start. Paused time does not count."""
        end = self._paused_at if self._paused_at is not None else self._now()
        return end - self.start - self.paused_s

    def remaining(self) -> float:
        """The seconds of work left. Zero or less when the budget is used."""
        return self.budget_s - self.elapsed()
