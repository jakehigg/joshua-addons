"""The resume tool: a task that ran out of turns or out of time takes up its own work.

Each test gives the manager a work directory, and the checkout in it is a
directory, as a stopped worker leaves one."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ALEX, MIA, make_settings, mcp_session, payload
from joshua_developer import notify, server
from joshua_developer.locks import LockManager
from joshua_developer.manager import Manager
from joshua_developer.runtime import Report, StubRuntime, worker_name
from joshua_developer.store import TaskStore
from joshua_developer.worker_api import build_brief

REPO = "github.com/example-home/app"
# The error of a worker that used every turn of its persona.
TURNS = "the session ended error_max_turns: the worker used all 80 turns"


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.fixture
def manager(data_dir: Path, work_dir: Path):
    settings = make_settings(data_dir, work_dir=work_dir)
    store = TaskStore(settings.db_path)
    manager = Manager(settings, store, LockManager())
    manager.runtime = StubRuntime(manager, delay_s=None)
    yield manager
    store.close()


@pytest.fixture
def resume_app(data_dir: Path, work_dir: Path):
    """The app with work directories, and a stub runtime that records no report."""
    from joshua_developer.server import build_app

    return build_app(make_settings(data_dir, work_dir=work_dir), stub_delay_s=None)


def report(status: str = "failed", error: str | None = TURNS) -> Report:
    return Report(
        status=status,  # type: ignore[arg-type]
        summary="Added the endpoint. The tests still fail.",
        error=error,
        log="first worker log",
    )


async def stopped_task(
    manager: Manager,
    result: Report | None = None,
    person: str = "alex",
    branch: str = "feature",
    checkout: bool = True,
) -> str:
    """A task of ``person`` that ended, with a checkout in its work directory."""
    started = await manager.develop(person, REPO, "Add a health check.", branch=branch)
    task_id = started["task_id"]
    if checkout:
        (manager.workspaces.path(task_id) / "repo" / ".git").mkdir(parents=True, exist_ok=True)
    await manager.record_report(task_id, result or report())
    manager.runtime._active.discard(task_id)  # type: ignore[union-attr]
    return task_id


# --- a resume ---------------------------------------------------------------


async def test_a_task_that_ran_out_of_turns_resumes_on_its_own_work(manager, work_dir) -> None:
    task_id = await stopped_task(manager)
    first = manager.store.get_task_full(task_id)
    assert first is not None and first["status"] == "failed"
    assert manager.resumable(task_id) is True

    result = await manager.resume("alex", {"task_id": task_id}, note="Leave the lint alone.")
    assert result["task_id"] == task_id
    assert result["status"] == "dispatched"
    assert result["resumed"] is True
    assert "work in its directory" in result["message"]

    full = manager.store.get_task_full(task_id)
    assert full is not None
    # The stub runtime marks the new worker running at once.
    assert full["status"] == "running"
    assert full["resumes"] == 1
    assert full["worker_token"] and full["worker_token"] != first["worker_token"]
    assert full["repo"] == REPO and full["branch_name"] == "feature"
    assert full["persona"] == first["persona"]
    assert full["report"] is None and full["error"] is None
    assert [entry["status"] for entry in full["history"]] == ["failed"]
    assert full["history"][0]["log"] == "first worker log"
    lock = manager.locks.get(REPO, "branch:feature")
    assert lock is not None and lock.task_id == task_id
    assert worker_name(full) == f"dev-worker-{task_id[:8]}-r1"
    # The new worker keeps the work directory of the task.
    assert (work_dir / task_id / "repo" / ".git").is_dir()

    brief = build_brief(manager, full)
    assert brief["resumed"] is True
    assert brief["brief"] == "Add a health check."
    assert brief["branch"] == "feature"
    assert "Do not start the task over" in brief["answer"]
    assert "Added the endpoint" in brief["answer"]
    assert TURNS in brief["answer"]
    assert "Leave the lint alone." in brief["answer"]


async def test_a_resume_takes_another_persona(manager) -> None:
    task_id = await stopped_task(manager)
    result = await manager.resume("alex", {"task_id": task_id}, persona="fable")
    assert result["status"] == "dispatched"
    full = manager.store.get_task_full(task_id)
    assert full is not None and full["persona"] == "fable"
    assert build_brief(manager, full)["persona"]["name"] == "fable"


async def test_a_resume_with_an_unknown_persona_is_refused(manager) -> None:
    task_id = await stopped_task(manager)
    result = await manager.resume("alex", {"task_id": task_id}, persona="nope")
    assert result["status"] == "rejected" and result["reason"] == "unknown_persona"
    full = manager.store.get_task_full(task_id)
    assert full is not None and full["status"] == "failed" and full["resumes"] == 0


async def test_a_timed_out_task_resumes_twice_and_then_hits_the_cap(manager) -> None:
    task_id = await stopped_task(manager, report(status="timed_out", error=None))
    for count in (1, 2):
        result = await manager.resume("alex", {"task_id": task_id})
        assert result["status"] == "dispatched", result
        full = manager.store.get_task_full(task_id)
        assert full is not None and full["resumes"] == count
        await manager.record_report(task_id, report())
        manager.runtime._active.discard(task_id)  # type: ignore[union-attr]

    assert manager.resumable(task_id) is False
    refused = await manager.resume("alex", {"task_id": task_id})
    assert refused["status"] == "rejected" and refused["reason"] == "not_resumable"
    assert "work.max_resumes" in refused["message"]
    assert "task_output" in refused["message"]
    full = manager.store.get_task_full(task_id)
    assert full is not None and full["status"] == "failed" and full["resumes"] == 2
    assert len(full["history"]) == 2


async def test_a_note_that_is_too_long_is_refused(manager) -> None:
    task_id = await stopped_task(manager)
    result = await manager.resume("alex", {"task_id": task_id}, note="x" * 100_001)
    assert result["status"] == "rejected" and result["reason"] == "invalid_arguments"
    assert "note is longer" in result["message"]


# --- the refusals -----------------------------------------------------------


async def test_a_task_that_ended_success_is_not_resumable(manager) -> None:
    task_id = await stopped_task(manager, Report(status="success", summary="Done.", pushed=True))
    assert manager.resumable(task_id) is False
    result = await manager.resume("alex", {"task_id": task_id})
    assert result["status"] == "rejected" and result["reason"] == "not_resumable"
    assert "is success" in result["message"]


async def test_an_end_that_is_not_the_turn_limit_or_a_timeout_is_not_resumable(manager) -> None:
    task_id = await stopped_task(
        manager, report(error="the diff holds what looks like a credential (github-pat in a.py:3)")
    )
    assert manager.resumable(task_id) is False
    result = await manager.resume("alex", {"task_id": task_id})
    assert result["status"] == "rejected" and result["reason"] == "not_resumable"
    assert "did not end at the turn limit" in result["message"]


async def test_a_task_whose_work_directory_is_gone_is_not_resumable(manager) -> None:
    task_id = await stopped_task(manager, checkout=False)
    assert manager.resumable(task_id) is False
    result = await manager.resume("alex", {"task_id": task_id})
    assert result["status"] == "rejected" and result["reason"] == "not_resumable"
    assert "work.keep_failed" in result["message"]


async def test_a_resume_with_no_work_directories_at_all_is_not_resumable(settings) -> None:
    store = TaskStore(settings.db_path)
    manager = Manager(settings, store, LockManager())
    manager.runtime = StubRuntime(manager, delay_s=None)
    try:
        task_id = await stopped_task(manager, checkout=False)
        result = await manager.resume("alex", {"task_id": task_id})
        assert result["status"] == "rejected" and result["reason"] == "not_resumable"
        assert "DEVELOPER_WORK_DIR" in result["message"]
    finally:
        store.close()


async def test_a_task_at_a_zero_cap_is_not_resumable(data_dir, work_dir) -> None:
    settings = make_settings(data_dir, work_dir=work_dir, config={"work": {"max_resumes": 0}})
    store = TaskStore(settings.db_path)
    manager = Manager(settings, store, LockManager())
    manager.runtime = StubRuntime(manager, delay_s=None)
    try:
        task_id = await stopped_task(manager)
        result = await manager.resume("alex", {"task_id": task_id})
        assert result["status"] == "rejected" and result["reason"] == "not_resumable"
        assert "work.max_resumes" in result["message"]
    finally:
        store.close()


async def test_another_persons_task_is_not_found(resume_app) -> None:
    manager = server._get_manager()
    task_id = await stopped_task(manager, person="alex")
    async with mcp_session(resume_app, MIA) as session:
        result = await session.call_tool("resume", {"task_id": task_id})
        status = await session.call_tool("task_status", {"task_id": task_id})
    assert result.is_error is True and f"404 not found: no task {task_id}" in result.content[0].text
    assert status.is_error is True


# --- the tools --------------------------------------------------------------


async def test_the_resume_tool_starts_a_worker_and_task_status_tells_the_caller(
    resume_app,
) -> None:
    manager = server._get_manager()
    async with mcp_session(resume_app, ALEX) as session:
        task_id = await stopped_task(manager)
        before = payload(await session.call_tool("task_status", {"task_id": task_id}))
        resumed = payload(await session.call_tool("resume", {"task_id": task_id, "note": "Go on."}))
        after = payload(await session.call_tool("task_status", {"task_id": task_id}))
        output = payload(await session.call_tool("task_output", {"task_id": task_id}))
    assert before["resumable"] is True and before["resumes"] == 0
    assert resumed["status"] == "dispatched" and resumed["resumed"] is True
    # A task that runs takes no resume: it has not ended.
    assert after["status"] == "running" and after["resumable"] is False
    assert after["resumes"] == 1
    assert [entry["error"] for entry in output["history"]] == [TURNS]


async def test_the_report_of_a_resumable_task_says_that_it_can_be_resumed(
    manager, monkeypatch
) -> None:
    sent: list[str] = []

    async def fake_event(settings, destination, text, *args, **kwargs) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(notify, "send_event", fake_event)
    await stopped_task(manager)
    assert len(sent) == 1
    assert "The task can be resumed" in sent[0]

    sent.clear()
    await stopped_task(manager, report(error="the pull request 7 is gone"), branch="other")
    assert len(sent) == 1
    assert "The task can be resumed" not in sent[0]
