"""Run the sync once a day, at ``VINYL_SYNC_TIME``, inside the one container.

The loop sleeps until the next occurrence of the configured local time,
runs the job in a worker thread, logs the outcome, and sleeps again. A
failed run is logged and the next run still happens.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from joshua_vinyl.log import get_logger

logger = get_logger("vinyl.scheduler")


def parse_time(text: str) -> tuple[int, int]:
    """``HH:MM`` as ``(hour, minute)``. Raises ``ValueError`` for anything else."""
    parts = text.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"expected HH:MM, got {text!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"expected HH:MM, got {text!r}")
    return hour, minute


def seconds_until(sync_time: str, now: datetime) -> float:
    """Seconds from ``now`` to the next occurrence of ``sync_time`` (local time)."""
    hour, minute = parse_time(sync_time)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def run_daily(
    sync_time: str,
    job: Callable[[], object],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], datetime] = datetime.now,
    runs: int | None = None,
) -> int:
    """Run ``job`` at ``sync_time`` every day. ``runs`` limits the loop, for a test."""
    done = 0
    while runs is None or done < runs:
        delay = seconds_until(sync_time, now())
        logger.info({"message": "next sync scheduled", "in_seconds": round(delay)})
        await sleep(delay)
        try:
            await asyncio.to_thread(job)
        except Exception:
            logger.exception({"message": "scheduled sync failed"})
        done += 1
    return done
