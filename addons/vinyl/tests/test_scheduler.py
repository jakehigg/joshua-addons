"""The nightly schedule: the time parser, the countdown, and the loop."""

from __future__ import annotations

from datetime import datetime

import pytest
from joshua_vinyl import scheduler


@pytest.mark.parametrize(("text", "expected"), [("03:00", (3, 0)), (" 23:59 ", (23, 59))])
def test_parse_time(text: str, expected: tuple[int, int]) -> None:
    assert scheduler.parse_time(text) == expected


@pytest.mark.parametrize("text", ["3", "24:00", "12:60", "abc", "1:2:3"])
def test_parse_time_refuses_bad_input(text: str) -> None:
    with pytest.raises(ValueError):
        scheduler.parse_time(text)


def test_seconds_until_later_today() -> None:
    now = datetime(2026, 9, 8, 1, 30)
    assert scheduler.seconds_until("03:00", now) == 5400


def test_seconds_until_wraps_to_tomorrow() -> None:
    now = datetime(2026, 9, 8, 3, 0, 1)
    assert scheduler.seconds_until("03:00", now) == 86399


async def test_run_daily_sleeps_then_runs_and_survives_a_failure() -> None:
    slept: list[float] = []
    ran: list[int] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    def job() -> None:
        ran.append(1)
        if len(ran) == 1:
            raise RuntimeError("first run fails")

    now = datetime(2026, 9, 8, 2, 0)
    done = await scheduler.run_daily("03:00", job, sleep=sleep, now=lambda: now, runs=2)
    assert done == 2
    assert slept == [3600, 3600]
    assert ran == [1, 1]
