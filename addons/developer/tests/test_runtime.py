"""The stub runtime: it starts no worker and records a fake report."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from joshua_developer.runtime import (
    Deadline,
    Report,
    StubRuntime,
    TaskClock,
    kill_time,
    no_start_report,
)


class Recorder:
    def __init__(self) -> None:
        self.running: list[str] = []
        self.reports: list[tuple[str, Report]] = []

    def mark_running(self, task_id: str) -> None:
        self.running.append(task_id)

    async def record_report(self, task_id: str, report: Report) -> dict:
        self.reports.append((task_id, report))
        return {}


TASK = {"task_id": "t1", "branch_name": "feature"}


async def test_no_delay_reports_before_start_returns() -> None:
    recorder = Recorder()
    runtime = StubRuntime(recorder, delay_s=0)
    handle = await runtime.start(TASK)
    assert handle == "stub-t1"
    assert recorder.running == ["t1"]
    [(task_id, report)] = recorder.reports
    assert task_id == "t1"
    assert report.status == "success"
    assert report.pushed is False
    assert runtime.count_active() == 0


async def test_a_delay_reports_later() -> None:
    recorder = Recorder()
    runtime = StubRuntime(recorder, delay_s=0.01)
    await runtime.start(TASK)
    assert runtime.count_active() == 1
    assert recorder.reports == []
    for _ in range(100):
        if recorder.reports:
            break
        await asyncio.sleep(0.01)
    assert len(recorder.reports) == 1
    assert runtime.count_active() == 0


async def test_none_holds_the_task() -> None:
    recorder = Recorder()
    runtime = StubRuntime(recorder, delay_s=None)
    await runtime.start(TASK)
    await asyncio.sleep(0)
    assert runtime.count_active() == 1
    assert recorder.reports == []


def test_a_report_refuses_an_unknown_field_or_status() -> None:
    with pytest.raises(ValueError):
        Report(status="success", token="x")  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        Report(status="in_progress")  # type: ignore[arg-type]


T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def test_kill_time_without_a_wait() -> None:
    clock = TaskClock(started_at=T0)
    assert kill_time(clock, 1200, 120, at(-50), at(0)) == at(1320)
    # No row: the start of the supervisor counts.
    assert kill_time(None, 1200, 120, at(-50), at(0)) == at(1270)
    assert kill_time(TaskClock(started_at=None), 1200, 120, at(5), at(0)) == at(1325)


def test_kill_time_adds_the_waits_that_ended() -> None:
    clock = TaskClock(started_at=T0, paused_s=600)
    assert kill_time(clock, 1200, 120, T0, at(1500)) == at(1920)


def test_kill_time_moves_with_an_open_wait() -> None:
    clock = TaskClock(started_at=T0, paused_s=100, asked_at=at(1000))
    # 4000 s into the wait: the deadline is 4000 s later than with no wait.
    assert kill_time(clock, 1200, 120, T0, at(5000)) == at(1320 + 100 + 4000)
    # The deadline stays ahead of now while the wait is open.
    for now in (1000, 3000, 9000):
        assert kill_time(clock, 1200, 120, T0, at(now)) > at(now)
    # A clock that runs behind asked_at adds nothing.
    assert kill_time(clock, 1200, 120, T0, at(900)) == at(1420)


class ClockReporter:
    def __init__(self) -> None:
        self.clock: TaskClock | None = None

    def task_clock(self, task_id: str) -> TaskClock | None:
        return self.clock


def test_the_deadline_reads_the_row_on_each_call() -> None:
    now = [T0]
    reporter = ClockReporter()
    deadline = Deadline(reporter, "t1", 1200, 7200, 120, now=lambda: now[0])  # type: ignore[arg-type]
    reporter.clock = TaskClock(started_at=T0)
    now[0] = at(1319)
    assert deadline.check() is None
    now[0] = at(1320)
    assert deadline.check() == "deadline"
    reporter.clock = TaskClock(started_at=T0, paused_s=300)
    assert deadline.check() is None
    reporter.clock = TaskClock(started_at=T0, asked_at=at(1000))
    now[0] = at(5000)
    assert deadline.check() is None


def test_the_hard_cap_is_timeout_plus_ask_wait_plus_grace_after_the_start() -> None:
    now = [at(-900)]
    reporter = ClockReporter()
    # The worker read its brief 900 s after its start; a wait is open since then.
    reporter.clock = TaskClock(started_at=T0, asked_at=T0)
    deadline = Deadline(reporter, "t1", 1200, 7200, 120, now=lambda: now[0])  # type: ignore[arg-type]
    now[0] = at(1200 + 7200 + 120 - 1)
    assert deadline.check() is None
    now[0] = at(1200 + 7200 + 120)
    assert deadline.check() == "deadline"


def test_a_worker_that_has_not_read_its_brief_is_starting() -> None:
    now = [T0]
    reporter = ClockReporter()
    reporter.clock = TaskClock(started_at=None)
    deadline = Deadline(  # type: ignore[arg-type]
        reporter, "t1", 60, 60, 0, start_grace_s=600, now=lambda: now[0]
    )
    # Far past timeout_s + ask_wait_s: no clock runs before the brief.
    now[0] = at(599)
    assert deadline.check() is None
    now[0] = at(600)
    assert deadline.check() == "no_start"
    reporter.clock = None
    assert deadline.check() == "no_start"
    # The brief at 599 s starts the clock: the start grace no longer applies.
    reporter.clock = TaskClock(started_at=at(599))
    now[0] = at(599 + 60 + 0 - 1)
    assert deadline.check() is None
    now[0] = at(599 + 60)
    assert deadline.check() == "deadline"


def test_the_no_start_report_names_the_image_and_the_setting() -> None:
    report = no_start_report(600, "pulling")
    assert report.status == "failed"
    assert "did not start in 600 seconds" in (report.error or "")
    assert "WORKER_IMAGE" in (report.error or "")
    assert "WORKER_START_GRACE_S" in (report.error or "")
    assert report.log == "pulling"
