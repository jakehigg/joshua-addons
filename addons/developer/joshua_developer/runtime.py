"""The runtime: what starts a worker for a task.

A runtime starts one worker for one task and counts the workers that still
run. The worker, or the runtime for it, tells the manager what happens
through a ``Reporter``: the task runs, and later its report arrives.

``StubRuntime`` starts no container. It marks the task running and records a
fake successful report after ``delay_s`` seconds, so the whole flow runs
offline.

A supervisor of a real runtime stops a worker at its deadline. The deadline
moves while the worker waits for an answer: see ``Deadline``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from joshua_developer.store import utcnow


class Report(BaseModel):
    """The report a worker sends when its task ends.

    The report has no field for the branch or the pull request. The manager
    takes them from the task row and from the platform only. An unknown field
    is a validation error.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "failed", "blocked", "timed_out"]
    summary: str = ""
    files_changed: list[str] = Field(default_factory=list)
    tests_run: list[str] = Field(default_factory=list)
    open_question: str | None = None
    # True when the worker pushed the task's branch. The manager then scans
    # the diff and opens or finds the pull request.
    pushed: bool = False
    # The branch the worker pushed. It must be the task's branch.
    head: str | None = None
    # The commit of the branch after the checkout, before the session. The
    # scan reads the diff from this commit to the pushed branch.
    clone_head: str | None = None
    # True when the worker made the branch, because the remote did not have it.
    created_branch: bool = False
    commit_hash: str | None = None
    error: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost: float | None = None
    # The session log, for task_output. Never logged by the manager.
    log: str = ""


def worker_name(task: dict[str, Any]) -> str:
    """The container or Job name of the worker of ``task`` (the full row).

    A resumed task gets a new name for each worker, so a new worker never
    meets the name of the one before it.
    """
    name = f"dev-worker-{task['task_id'][:8]}"
    runs = len(task.get("history") or [])
    return f"{name}-r{runs}" if runs else name


@dataclass(frozen=True)
class TaskClock:
    """The clock fields of a task row."""

    started_at: datetime | None
    # The seconds of the waits for an answer that ended.
    paused_s: int = 0
    # The start of the open wait, or None.
    asked_at: datetime | None = None


def kill_time(
    clock: TaskClock | None,
    timeout_s: float,
    grace_s: float,
    fallback_start: datetime,
    now: datetime,
) -> datetime:
    """The time after which the supervisor stops the worker.

    ``started_at + timeout_s + grace_s + paused_s``, plus the open wait so far.
    """
    clock = clock or TaskClock(started_at=None)
    paused = float(clock.paused_s)
    if clock.asked_at is not None:
        paused += max(0.0, (now - clock.asked_at).total_seconds())
    start = clock.started_at or fallback_start
    return start + timedelta(seconds=timeout_s + grace_s + paused)


class Deadline:
    """The deadline of one worker, for its supervisor.

    ``passed`` reads the task clock from the reporter on each call, so a wait
    for an answer moves the deadline. ``timeout_s + ask_wait_s + grace_s``
    after the start is a hard cap that no wait moves.
    """

    def __init__(
        self,
        reporter: Reporter,
        task_id: str,
        timeout_s: float,
        ask_wait_s: float,
        grace_s: float,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.reporter = reporter
        self.task_id = task_id
        self.timeout_s = timeout_s
        self.grace_s = grace_s
        self.now = now
        self.started = now()
        self.hard_cap = time.monotonic() + timeout_s + ask_wait_s + grace_s

    def passed(self) -> bool:
        if time.monotonic() >= self.hard_cap:
            return True
        now = self.now()
        clock = self.reporter.task_clock(self.task_id)
        return now >= kill_time(clock, self.timeout_s, self.grace_s, self.started, now)


class Reporter(Protocol):
    """The manager side that a runtime reports to."""

    def is_active(self, task_id: str, worker_token: str | None = None) -> bool:
        """True while the task has no report.

        With ``worker_token``, True only while the task still has that token.
        """
        ...

    def mark_running(self, task_id: str) -> None: ...

    def task_clock(self, task_id: str) -> TaskClock | None:
        """The clock fields of the task, or None when it is not known."""
        ...

    async def record_report(self, task_id: str, report: Report) -> dict[str, Any] | None: ...


class Runtime(Protocol):
    """Starts workers and counts the ones that run."""

    async def start(self, task: dict[str, Any]) -> str:
        """Start a worker for ``task`` (the full task row). Returns a handle."""
        ...

    def count_active(self) -> int:
        """The number of workers that were started and have not ended."""
        ...


class StubRuntime:
    """A runtime that starts no worker and records a fake successful report.

    ``delay_s`` 0 records the report before ``start`` returns. A positive
    value records it later, from a background task. None never records one,
    so the task stays running.
    """

    def __init__(self, reporter: Reporter, delay_s: float | None = 1.0) -> None:
        self.reporter = reporter
        self.delay_s = delay_s
        self._active: set[str] = set()
        self._background: set[asyncio.Task[None]] = set()

    def count_active(self) -> int:
        return len(self._active)

    async def start(self, task: dict[str, Any]) -> str:
        task_id = task["task_id"]
        self._active.add(task_id)
        self.reporter.mark_running(task_id)
        if self.delay_s is None:
            return f"stub-{task_id}"
        if self.delay_s <= 0:
            await self._finish(task)
        else:
            job = asyncio.create_task(self._finish_later(task))
            self._background.add(job)
            job.add_done_callback(self._background.discard)
        return f"stub-{task_id}"

    async def _finish_later(self, task: dict[str, Any]) -> None:
        await asyncio.sleep(self.delay_s or 0)
        await self._finish(task)

    async def _finish(self, task: dict[str, Any]) -> None:
        report = Report(
            status="success",
            summary="The stub runtime ran no worker. This report is fake.",
            commit_hash="0" * 40,
            input_tokens=0,
            output_tokens=0,
            estimated_cost=0.0,
            log="stub runtime: no session ran",
        )
        try:
            await self.reporter.record_report(task["task_id"], report)
        finally:
            self._active.discard(task["task_id"])
