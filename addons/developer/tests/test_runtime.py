"""The stub runtime: it starts no worker and records a fake report."""

from __future__ import annotations

import asyncio

import pytest
from joshua_developer.runtime import Report, StubRuntime


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
    assert report.branch == "feature"
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
