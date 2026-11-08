"""The task clock: a work budget that pauses while ``ask`` waits."""

from __future__ import annotations

from joshua_developer_worker.clock import Clock


class Now:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_a_clock_counts_work_from_its_start() -> None:
    now = Now()
    clock = Clock(60, now=now)
    assert clock.start == 1000.0
    now.t += 20
    assert clock.elapsed() == 20
    assert clock.remaining() == 40
    now.t += 50
    assert clock.remaining() == -10


def test_a_start_in_the_past_counts() -> None:
    now = Now()
    clock = Clock(60, start=990.0, now=now)
    assert clock.elapsed() == 10


def test_paused_time_does_not_count() -> None:
    now = Now()
    clock = Clock(60, now=now)
    now.t += 10
    clock.pause()
    assert clock.paused
    now.t += 3600
    assert clock.elapsed() == 10
    assert clock.remaining() == 50
    clock.resume()
    assert not clock.paused
    assert clock.paused_s == 3600
    now.t += 5
    assert clock.elapsed() == 15
    clock.pause()
    now.t += 100
    clock.resume()
    assert clock.paused_s == 3700
    assert clock.remaining() == 45


def test_a_second_pause_and_a_lone_resume_change_nothing() -> None:
    now = Now()
    clock = Clock(60, now=now)
    clock.resume()
    assert clock.paused_s == 0
    clock.pause()
    now.t += 10
    clock.pause()
    now.t += 10
    clock.resume()
    assert clock.paused_s == 20
    assert clock.elapsed() == 0
