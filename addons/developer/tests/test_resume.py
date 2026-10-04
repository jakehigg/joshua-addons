"""Resume: an answer to a task that stopped starts a new worker on its branch.
Also the ask of a worker that nobody can get."""

from __future__ import annotations

import httpx
import pytest
from conftest import ALEX, mcp_session, payload
from joshua_developer import notify, server
from joshua_developer.locks import LockManager
from joshua_developer.manager import Manager
from joshua_developer.runtime import Report, StubRuntime, worker_name
from joshua_developer.store import TaskStore
from joshua_developer.worker_api import build_brief, build_worker_app

REPO = "github.com/example-home/app"


@pytest.fixture
def manager(settings):
    store = TaskStore(settings.db_path)
    manager = Manager(settings, store, LockManager())
    manager.runtime = StubRuntime(manager, delay_s=None)
    yield manager
    store.close()


def blocked(pushed: bool = True, status: str = "blocked") -> Report:
    return Report(
        status=status,  # type: ignore[arg-type]
        summary="Stopped: blocked on a question. The work so far is on branch feature.",
        open_question="Which port?" if status == "blocked" else None,
        branch="feature",
        pushed=pushed,
        head="feature" if pushed else None,
        commit_hash="a" * 40 if pushed else None,
        log="first worker log",
    )


async def stopped_task(manager: Manager, report: Report | None = None) -> str:
    result = await manager.develop("alex", REPO, "Add a health check.", branch="feature")
    task_id = result["task_id"]
    await finish(manager, task_id, report or blocked())
    return task_id


async def finish(manager: Manager, task_id: str, report: Report) -> None:
    """Record a report, and let the stub runtime count the worker as gone."""
    await manager.record_report(task_id, report)
    manager.runtime._active.discard(task_id)  # type: ignore[union-attr]


async def test_an_answer_resumes_a_blocked_task_end_to_end(manager, git_host) -> None:
    task_id = await stopped_task(manager)
    # The first worker waited for answers; the new worker starts a fresh clock.
    manager.store.update_task(task_id, paused_s=900, asked_at="2026-10-04T12:00:00+00:00")
    first = manager.store.get_task_full(task_id)
    assert first is not None and first["status"] == "blocked"
    old_token = first["worker_token"]
    assert manager.locks.get(REPO, "branch:feature") is None

    result = await manager.answer("alex", {"task_id": task_id}, "Port 8080.")
    assert result["task_id"] == task_id
    assert result["status"] == "dispatched"
    assert result["resumed"] is True

    full = manager.store.get_task_full(task_id)
    assert full is not None
    # The stub runtime marks the new worker running at once.
    assert full["status"] == "running"
    assert full["started_at"] is not None and full["completed_at"] is None
    assert full["worker_token"] and full["worker_token"] != old_token
    assert full["open_question"] is None and full["report"] is None
    assert full["answer"] == "Port 8080."
    assert full["paused_s"] == 0 and full["asked_at"] is None
    assert [entry["status"] for entry in full["history"]] == ["blocked"]
    assert full["history"][0]["pushed"] is True
    assert full["history"][0]["completed_at"] == first["completed_at"]
    lock = manager.locks.get(REPO, "branch:feature")
    assert lock is not None and lock.task_id == task_id
    # The supervisor of the first worker no longer reports for the task.
    assert manager.is_active(task_id)
    assert not manager.is_active(task_id, old_token)
    assert manager.is_active(task_id, full["worker_token"])
    assert worker_name(full) == f"dev-worker-{task_id[:8]}-r1"

    brief = build_brief(manager, full)
    assert brief["resumed"] is True
    assert brief["answer"] == "Port 8080."
    assert brief["note"] == first["summary"]
    assert brief["branch"] == "feature" and brief["base_branch"] == "main"
    assert brief["brief"] == "Add a health check."

    # The old token reaches nothing; the new one gets the brief.
    app = build_worker_app(manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://d") as client:
        old = await client.get("/worker/brief", headers={"Authorization": f"Bearer {old_token}"})
        new = await client.get(
            "/worker/brief", headers={"Authorization": f"Bearer {full['worker_token']}"}
        )
    assert old.status_code == 401
    assert new.json()["resumed"] is True

    done = Report(status="success", summary="Done.", branch="feature", pushed=True, head="feature")
    task = await manager.record_report(task_id, done)
    assert task is not None and task["status"] == "success"
    assert task["pr_number"] == 42
    final = manager.store.get_task_full(task_id)
    assert final is not None
    assert final["report"]["summary"] == "Done."
    assert len(final["history"]) == 1 and final["history"][0]["log"] == "first worker log"
    assert manager.locks.get(REPO, "branch:feature") is None


async def test_a_timed_out_task_resumes_and_a_later_stop_resumes_again(manager) -> None:
    task_id = await stopped_task(manager, blocked(status="timed_out"))
    first = await manager.answer("alex", {"task_id": task_id}, "Go on.")
    assert first["resumed"] is True
    # The second worker pushes nothing new, but the branch holds the first push.
    await finish(manager, task_id, blocked(pushed=False))
    again = await manager.answer("alex", {"task_id": task_id}, "Port 9090.")
    assert again["status"] == "dispatched"
    full = manager.store.get_task_full(task_id)
    assert full is not None and len(full["history"]) == 2
    assert worker_name(full).endswith("-r2")


async def test_a_task_that_pushed_nothing_is_not_resumable(manager) -> None:
    task_id = await stopped_task(manager, blocked(pushed=False))
    result = await manager.answer("alex", {"task_id": task_id}, "Port 8080.")
    assert result["status"] == "rejected"
    assert result["reason"] == "not_resumable"
    assert "pushed nothing" in result["message"]
    assert "develop again" in result["message"]
    full = manager.store.get_task_full(task_id)
    assert full is not None and full["status"] == "blocked" and full["history"] is None


async def test_a_finished_task_takes_no_answer(manager) -> None:
    task_id = await stopped_task(manager, Report(status="success", summary="ok"))
    result = await manager.answer("alex", {"task_id": task_id}, "late")
    assert result["reason"] == "not_waiting"


async def test_develop_on_the_same_branch_after_a_blocked_task_then_resume_is_locked(
    manager,
) -> None:
    task_id = await stopped_task(manager)
    second = await manager.develop("alex", REPO, "Carry on.", branch="feature")
    assert second["status"] == "dispatched"
    assert second["task_id"] != task_id
    old = manager.store.get_task(task_id)
    assert old is not None and old["status"] == "blocked"

    result = await manager.answer("alex", {"task_id": task_id}, "Port 8080.")
    assert result["status"] == "rejected"
    assert result["reason"] == "locked"
    assert result["lock_info"]["task_id"] == second["task_id"]
    full = manager.store.get_task_full(task_id)
    assert full is not None and full["status"] == "blocked" and full["answer"] is None


async def test_resume_obeys_the_concurrency_limit(manager) -> None:
    task_id = await stopped_task(manager)
    for branch in ("one", "two"):
        started = await manager.develop("alex", REPO, "Other work.", branch=branch)
        assert started["status"] == "dispatched"
    result = await manager.answer("alex", {"task_id": task_id}, "Port 8080.")
    assert result["status"] == "rejected"
    assert result["reason"] == "concurrency_limit"
    assert manager.locks.get(REPO, "branch:feature") is None


class BrokenRuntime:
    def count_active(self) -> int:
        return 0

    async def start(self, task: dict) -> str:
        raise OSError("no")


async def test_a_resume_that_cannot_start_fails_the_task(manager) -> None:
    task_id = await stopped_task(manager)
    manager.runtime = BrokenRuntime()
    result = await manager.answer("alex", {"task_id": task_id}, "Port 8080.")
    assert result == {
        "task_id": task_id,
        "status": "failed",
        "error": "the worker did not start",
        "resumed": True,
    }
    assert manager.locks.get(REPO, "branch:feature") is None


async def test_the_answer_tool_resumes_and_task_output_shows_the_history(holding_app) -> None:
    async with mcp_session(holding_app, ALEX) as session:
        started = payload(
            await session.call_tool(
                "develop", {"repo": REPO, "brief": "Add a health check.", "branch": "feature"}
            )
        )
        task_id = started["task_id"]
        await finish(server._get_manager(), task_id, blocked())
        resumed = payload(
            await session.call_tool("answer", {"task_id": task_id, "text": "Port 8080."})
        )
        output = payload(await session.call_tool("task_output", {"task_id": task_id}))
    assert resumed["resumed"] is True and resumed["status"] == "dispatched"
    assert output["status"] == "running"
    assert output["report"] is None
    assert [entry["open_question"] for entry in output["history"]] == ["Which port?"]


# --- ask with nobody to reach -----------------------------------------------


async def test_ask_with_no_destination_stores_the_question(manager, monkeypatch) -> None:
    calls: list[str] = []

    async def fake_question(*args, **kwargs) -> bool:  # pragma: no cover - never called
        calls.append("sent")
        return True

    monkeypatch.setattr(notify, "send_question", fake_question)
    result = await manager.develop("mia", REPO, "Add a health check.")
    full = manager.store.get_task_full(result["task_id"])
    assert full is not None
    app = build_worker_app(manager)
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {full['worker_token']}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://d") as client:
        asked = await client.post("/worker/ask", json={"question": "Which port?"}, headers=headers)
    assert asked.status_code == 202
    assert asked.json() == {"asked": True, "sent": False, "reason": "no_destination"}
    assert calls == []
    task = manager.store.get_task(result["task_id"])
    assert task is not None and task["open_question"] == "Which port?"


async def test_ask_when_the_send_fails(manager, monkeypatch) -> None:
    async def failed_question(*args, **kwargs) -> bool:
        return False

    monkeypatch.setattr(notify, "send_question", failed_question)
    result = await manager.develop("alex", REPO, "Add a health check.")
    sent, reason = await manager.ask(result["task_id"], "Which port?")
    assert (sent, reason) == (False, "send_failed")
    task = manager.store.get_task(result["task_id"])
    assert task is not None and task["open_question"] == "Which port?"
