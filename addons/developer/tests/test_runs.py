"""Development auto-run: the tools, the step after each report, and the tasks and limits calls."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import httpx
import pytest
from conftest import ADDON, MIA, error, make_settings, mcp_session, mcp_sessions, payload
from joshua_developer import notify, runs
from joshua_developer.locks import LockManager
from joshua_developer.manager import Manager
from joshua_developer.origin import WORKSPACE_META_KEY
from joshua_developer.runs import (
    TasksClient,
    TasksError,
    append_note,
    brief_of,
)
from joshua_developer.runtime import Report, StubRuntime
from joshua_developer.server import build_app
from joshua_developer.store import TaskStore

REPO = "github.com/example-home/app"
PR = "https://github.com/example-home/app/pull/42"
WORKSPACE = "6f1c2d1e-0b1a-4c55-9d7e-2f3a4b5c6d7e"
G1, G2 = str(uuid.uuid4()), str(uuid.uuid4())


class FakeServices:
    """A fake tasks addon, a fake core, and a fake channels, in one handler."""

    def __init__(self) -> None:
        self.tasks: list[dict[str, Any]] = []
        self.groups = {G1: "Phase one", G2: "Phase two"}
        self.project_id = str(uuid.uuid4())
        self.limits: httpx.Response | Exception = httpx.Response(404)
        self.tasks_error: Exception | None = None
        self.events: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []

    def add(self, number: int, title: str, group: str = G1, notes: str = "") -> dict[str, Any]:
        task = {
            "id": str(uuid.uuid4()),
            "number": number,
            "title": title,
            "notes": notes,
            "done": False,
            "group_id": group,
            "blocked": False,
        }
        self.tasks.append(task)
        return task

    def by_number(self, number: int) -> dict[str, Any]:
        return next(t for t in self.tasks if t["number"] == number)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "channels":
            self.events.append(json.loads(request.content))
            return httpx.Response(202, json={"accepted": True})
        if host == "core":
            assert path == "/v1/limits"
            if isinstance(self.limits, Exception):
                raise self.limits
            return self.limits
        if self.tasks_error is not None:
            raise self.tasks_error
        assert host == "tasks", host
        if request.method == "GET" and path == "/api/next-steps":
            ref = request.url.params["project"]
            if ref not in (self.project_id, "chat", "feat-chat", "Chat Project"):
                return httpx.Response(404, json={"detail": "no project"})
            open_tasks = [t for t in self.tasks if not t["done"]]
            ready = [t for t in open_tasks if not t["blocked"]][:1]
            return httpx.Response(
                200,
                json=[
                    {
                        "id": self.project_id,
                        "name": "Chat Project",
                        "slug": "chat",
                        "open_count": len(open_tasks),
                        "tasks": ready,
                    }
                ],
            )
        if request.method == "GET" and path.startswith("/api/tasks/by-number/"):
            return httpx.Response(200, json=self.by_number(int(path.rsplit("/", 1)[1])))
        if request.method == "PATCH" and path.startswith("/api/tasks/"):
            task_id = path.rsplit("/", 1)[1]
            task = next(t for t in self.tasks if t["id"] == task_id)
            task.update(json.loads(request.content))
            return httpx.Response(200, json=task)
        if request.method == "GET" and path == f"/api/projects/{self.project_id}/groups":
            return httpx.Response(200, json=[{"id": k, "name": v} for k, v in self.groups.items()])
        return httpx.Response(404)

    def texts(self, verbatim: bool | None = None) -> list[str]:
        return [
            e["text"]
            for e in self.events
            if verbatim is None or bool(e.get("verbatim")) is verbatim
        ]


@pytest.fixture
def services(monkeypatch) -> FakeServices:
    fake = FakeServices()

    def client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))

    monkeypatch.setattr(runs, "make_client", client)
    monkeypatch.setattr(notify, "_make_client", client)
    monkeypatch.setattr(notify, "RETRY_BACKOFF_S", 0)
    return fake


@pytest.fixture
def settings(data_dir):
    return make_settings(
        data_dir,
        channels_url="http://channels:8000",
        channels_token="fleet",
        tasks_url="http://tasks:8000",
        core_url="http://core:8000",
    )


_stores: list[TaskStore] = []


@pytest.fixture(autouse=True)
def close_stores():
    yield
    while _stores:
        _stores.pop().close()


def make_manager(settings) -> Manager:
    store = TaskStore(settings.db_path)
    _stores.append(store)
    manager = Manager(settings, store, LockManager())
    # No report comes by itself: each test records the reports.
    manager.runtime = StubRuntime(manager, delay_s=None)
    return manager


def current(manager: Manager, run_id: str) -> dict[str, Any]:
    task_id = manager.store.get_run(run_id)["current_task_id"]  # type: ignore[index]
    full = manager.store.get_task_full(task_id)
    assert full is not None
    return full


async def finish(
    manager: Manager, run_id: str, status: str = "success", question: str | None = None, **row: Any
) -> None:
    """End the current task of a run, with a pull request on the row."""
    task = current(manager, run_id)
    if row:
        manager.store.update_task(task["task_id"], **row)
    await manager.record_report(
        task["task_id"], Report(status=status, summary="done", open_question=question)
    )


async def started(manager, services, **kwargs) -> str:
    result = await manager.runs.start(
        "alex", REPO, "chat", workspace=kwargs.pop("workspace", None), **kwargs
    )
    assert result["status"] == "active", result
    return result["run_id"]


# --- starting a run --------------------------------------------------------


async def test_a_run_needs_the_tasks_url(settings, services) -> None:
    manager = make_manager(make_settings(settings.data_dir, tasks_url=None))
    result = await manager.runs.start("alex", REPO, "chat")
    assert result["reason"] == "tasks_not_configured"
    assert "TASKS_URL" in result["message"]


async def test_run_project_dispatches_the_first_task(settings, services) -> None:
    services.add(1, "Add the check", notes="Use the old path.\ndone in https://x/pull/1")
    services.add(2, "Second")
    manager = make_manager(settings)
    result = await manager.runs.start("alex", REPO, "chat", workspace=WORKSPACE, notify_to="d1")
    assert result["branch"] == "feat-chat"
    assert result["task"] == {"number": 1, "title": "Add the check"}
    task = current(manager, result["run_id"])
    assert task["task_type"] == "develop"
    assert task["branch_name"] == "feat-chat"
    assert task["workspace"] == WORKSPACE
    assert task["notify"] == "d1"
    assert task["tasks_task_number"] == 1
    assert task["tasks_task_id"] == services.by_number(1)["id"]
    assert task["brief"] == "Add the check\n\nUse the old path.\n\nCommit and push your work."
    run = manager.store.get_run(result["run_id"])
    assert run["status"] == "active" and run["current_task_number"] == 1  # type: ignore[index]
    note = services.by_number(1)["notes"].splitlines()[-1]
    assert note == f"dispatched as developer task {task['task_id']} on feat-chat"


async def test_run_project_takes_a_given_branch_and_persona(settings, services) -> None:
    services.add(1, "One")
    manager = make_manager(settings)
    result = await manager.runs.start(
        "alex", REPO, "Chat Project", branch="feat-x", persona="fable"
    )
    assert result["branch"] == "feat-x"
    assert current(manager, result["run_id"])["persona"] == "fable"


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        ({"project": "nope"}, "project_not_found"),
        ({"project": " "}, "invalid_arguments"),
        ({"project": "chat", "branch": "bad branch"}, "invalid_arguments"),
        ({"project": "chat", "persona": "nobody"}, "unknown_persona"),
        ({"project": "chat", "repo": "github.com/other/app"}, "repo_not_allowed"),
    ],
)
async def test_run_project_refuses(settings, services, args, reason) -> None:
    services.add(1, "One")
    manager = make_manager(settings)
    call = {"repo": REPO, **args}
    result = await manager.runs.start("alex", call.pop("repo"), **call)
    assert result["status"] == "rejected" and result["reason"] == reason


async def test_run_project_refuses_with_no_ready_task(settings, services) -> None:
    manager = make_manager(settings)
    assert (await manager.runs.start("alex", REPO, "chat"))["reason"] == "no_tasks"
    services.add(1, "One")["blocked"] = True
    result = await manager.runs.start("alex", REPO, "chat")
    assert result["reason"] == "no_tasks" and "blocked" in result["message"]


async def test_run_project_refuses_when_tasks_is_down(settings, services) -> None:
    services.tasks_error = httpx.ConnectError("refused")
    manager = make_manager(settings)
    assert (await manager.runs.start("alex", REPO, "chat"))["reason"] == "tasks_unreachable"


async def test_a_second_run_on_the_same_project_is_refused(settings, services) -> None:
    services.add(1, "One")
    manager = make_manager(settings)
    run_id = await started(manager, services)
    again = await manager.runs.start("alex", REPO, "chat")
    assert again["reason"] == "run_exists" and again["run_id"] == run_id
    await manager.runs.stop(manager.store.get_run(run_id))  # type: ignore[arg-type]
    # A stopped run does not block a new one, once the branch lock is free.
    await finish(manager, run_id, status="failed")
    assert (await manager.runs.start("alex", REPO, "chat"))["status"] == "active"


async def test_a_rejected_first_dispatch_stops_the_run(settings, services) -> None:
    services.add(1, "One")
    manager = make_manager(settings)
    await manager.develop("alex", REPO, "x", branch="feat-chat")
    result = await manager.runs.start("alex", REPO, "chat")
    assert result["reason"] == "locked"
    [run] = manager.store.list_runs("alex")
    assert run["status"] == "stopped"


# --- the step --------------------------------------------------------------


async def test_a_success_marks_done_and_dispatches_the_next_task(settings, services) -> None:
    services.add(1, "One")
    services.add(2, "Two", notes="Detail.")
    manager = make_manager(settings)
    run_id = await started(manager, services, workspace=WORKSPACE)
    first = current(manager, run_id)
    await finish(manager, run_id, pr_url=PR, pr_number=42)
    one = services.by_number(1)
    assert one["done"] is True
    assert one["notes"].splitlines()[-1] == f"done in {PR}"
    run = manager.store.get_run(run_id)
    assert run["pr_number"] == 42 and run["pr_url"] == PR  # type: ignore[index]
    second = current(manager, run_id)
    assert second["task_id"] != first["task_id"]
    assert second["task_type"] == "rework" and second["pr_number"] == 42
    assert second["run_id"] == run_id and second["tasks_task_number"] == 2
    assert second["instructions"] == "Two\n\nDetail.\n\nCommit and push your work."
    assert second["workspace"] == WORKSPACE
    assert services.by_number(2)["notes"].splitlines()[-1].startswith("dispatched as developer")
    # One verbatim line, and no model turn for the report.
    assert [e.get("verbatim") for e in services.events] == [True]
    assert services.events[0]["text"] == "✓ #1 One — next: #2 Two"
    assert services.events[0]["workspace"] == WORKSPACE


async def test_the_last_task_ends_the_run_with_a_wake(settings, services) -> None:
    services.add(1, "One")
    manager = make_manager(settings)
    run_id = await started(manager, services)
    await finish(manager, run_id, pr_url=PR, pr_number=42)
    assert manager.store.get_run(run_id)["status"] == "done"  # type: ignore[index]
    [event] = services.events
    assert "verbatim" not in event
    assert "Project Chat Project finished" in event["text"] and PR in event["text"]


async def test_a_phase_end_pauses_and_resume_goes_on(settings, services) -> None:
    services.add(1, "One", G1)
    services.add(2, "Two", G2)
    manager = make_manager(settings)
    run_id = await started(manager, services)
    await finish(manager, run_id, pr_url=PR, pr_number=42)
    run = manager.store.get_run(run_id)
    assert run["status"] == "paused"  # type: ignore[index]
    [event] = services.events
    assert "verbatim" not in event
    assert "Phase Phase one done" in event["text"] and "resume" in event["text"]
    assert manager.store.count_active_tasks() == 0
    result = await manager.runs.resume(run)  # type: ignore[arg-type]
    assert result["status"] == "active"
    assert current(manager, run_id)["tasks_task_number"] == 2


@pytest.mark.parametrize(
    ("limits", "paused"),
    [
        (httpx.Response(200, json={"available": True, "five_hour": {"used_pct": 85}}), True),
        (httpx.Response(200, json={"available": True, "seven_day": {"used_pct": 90.0}}), True),
        (httpx.Response(200, json={"available": True, "five_hour": {"used_pct": 79.9}}), False),
        (httpx.Response(200, json={"available": False, "reason": "none"}), False),
        (httpx.Response(404), False),
        (httpx.Response(200, text="not json"), False),
        (httpx.ConnectError("refused"), False),
    ],
)
async def test_the_ceiling_pauses_and_unknown_runs(settings, services, limits, paused) -> None:
    services.limits = limits
    services.add(1, "One")
    services.add(2, "Two")
    manager = make_manager(settings)
    run_id = await started(manager, services)
    await finish(manager, run_id, pr_url=PR, pr_number=42)
    assert (manager.store.get_run(run_id)["status"] == "paused") is paused  # type: ignore[index]
    if paused:
        [event] = services.events
        assert "limit" in event["text"] and "verbatim" not in event
        assert manager.store.count_active_tasks() == 0
    else:
        assert current(manager, run_id)["tasks_task_number"] == 2


async def test_without_core_url_the_limits_are_unknown(settings, services) -> None:
    services.limits = httpx.Response(200, json={"available": True, "five_hour": {"used_pct": 99}})
    services.add(1, "One")
    services.add(2, "Two")
    manager = make_manager(make_settings(settings.data_dir, **_notify(settings), core_url=None))
    run_id = await started(manager, services)
    await finish(manager, run_id)
    assert current(manager, run_id)["tasks_task_number"] == 2
    assert not [r for r in services.requests if r.url.host == "core"]


def _notify(settings) -> dict[str, Any]:
    return {
        "channels_url": settings.channels_url,
        "channels_token": settings.channels_token,
        "tasks_url": settings.tasks_url,
    }


async def test_the_ceilings_come_from_developer_yaml(settings, services) -> None:
    services.limits = httpx.Response(200, json={"available": True, "five_hour": {"used_pct": 50}})
    services.add(1, "One")
    services.add(2, "Two")
    config = {"runs": {"ceiling_five_hour_pct": 40}}
    manager = make_manager(
        make_settings(
            settings.data_dir, config=config, **_notify(settings), core_url="http://core:8000"
        )
    )
    run_id = await started(manager, services)
    await finish(manager, run_id)
    assert manager.store.get_run(run_id)["status"] == "paused"  # type: ignore[index]


async def test_blocked_tasks_pause_the_run(settings, services) -> None:
    services.add(1, "One")
    services.add(2, "Two")
    manager = make_manager(settings)
    run_id = await started(manager, services)
    services.by_number(2)["blocked"] = True
    await finish(manager, run_id)
    assert manager.store.get_run(run_id)["status"] == "paused"  # type: ignore[index]
    assert "waits for a blocker" in services.events[0]["text"]


async def test_a_failure_is_tried_once_more_and_then_wakes(settings, services) -> None:
    services.add(1, "One")
    manager = make_manager(settings)
    run_id = await started(manager, services)
    first = current(manager, run_id)
    await finish(manager, run_id, status="failed", error="boom")
    again = current(manager, run_id)
    assert again["task_id"] != first["task_id"]
    assert again["tasks_task_number"] == 1 and again["task_type"] == "develop"
    assert manager.store.get_run(run_id)["retried"] is True  # type: ignore[index]
    assert [e.get("verbatim") for e in services.events] == [True]
    assert "failed" in services.events[0]["text"]
    assert "after a failure" in services.by_number(1)["notes"]
    await finish(manager, run_id, status="failed", error="boom again")
    run = manager.store.get_run(run_id)
    assert run["status"] == "paused"  # type: ignore[index]
    report = services.events[-1]
    assert "verbatim" not in report and "ended: failed" in report["text"]
    assert services.by_number(1)["done"] is False


async def test_a_success_clears_the_retry(settings, services) -> None:
    services.add(1, "One")
    services.add(2, "Two")
    manager = make_manager(settings)
    run_id = await started(manager, services)
    await finish(manager, run_id, status="failed")
    await finish(manager, run_id)
    assert manager.store.get_run(run_id)["retried"] is False  # type: ignore[index]
    assert current(manager, run_id)["tasks_task_number"] == 2


async def test_a_retry_that_cannot_start_pauses_with_the_report(settings, services) -> None:
    services.add(1, "One")
    manager = make_manager(settings)
    run_id = await started(manager, services)
    # Another task takes the lock of the branch, so the retry is refused.
    task = current(manager, run_id)
    manager.store.update_task(task["task_id"], status="failed")
    manager.locks.release(REPO, task["scope"], task["task_id"])
    await manager.develop("alex", REPO, "other", branch="feat-chat")
    manager.store.update_task(task["task_id"], status="running", completed_at=None)
    await manager.record_report(task["task_id"], Report(status="failed"))
    assert manager.store.get_run(run_id)["status"] == "paused"  # type: ignore[index]
    assert "ended: failed" in services.events[-1]["text"]


async def test_a_question_sends_the_report_and_keeps_the_run_active(settings, services) -> None:
    services.add(1, "One")
    manager = make_manager(settings)
    run_id = await started(manager, services)
    await finish(manager, run_id, status="blocked", question="Which port?")
    assert manager.store.get_run(run_id)["status"] == "active"  # type: ignore[index]
    [event] = services.events
    assert "verbatim" not in event and "Which port?" in event["text"]
    assert manager.store.get_run(run_id)["retried"] is False  # type: ignore[index]


async def test_a_task_with_no_run_sends_the_normal_report(settings, services) -> None:
    manager = make_manager(settings)
    task_id = (await manager.develop("alex", REPO, "brief", branch="feature"))["task_id"]
    await manager.record_report(task_id, Report(status="success", summary="ok"))
    [event] = services.events
    assert "verbatim" not in event and "ended: success" in event["text"]
    assert [r for r in services.requests if r.url.host == "tasks"] == []


async def test_a_paused_run_marks_done_and_goes_no_further(settings, services) -> None:
    services.add(1, "One")
    services.add(2, "Two")
    manager = make_manager(settings)
    run_id = await started(manager, services)
    await manager.runs.pause(manager.store.get_run(run_id))  # type: ignore[arg-type]
    await finish(manager, run_id)
    assert services.by_number(1)["done"] is True
    assert manager.store.count_active_tasks() == 0
    assert services.events[0]["verbatim"] is True and "paused" in services.events[0]["text"]


async def test_a_failure_of_a_stopped_run_sends_the_report(settings, services) -> None:
    services.add(1, "One")
    manager = make_manager(settings)
    run_id = await started(manager, services)
    await manager.runs.stop(manager.store.get_run(run_id))  # type: ignore[arg-type]
    await finish(manager, run_id, status="failed")
    assert manager.store.count_active_tasks() == 0
    assert "ended: failed" in services.events[0]["text"]


async def test_a_tasks_failure_at_the_step_pauses_the_run(settings, services) -> None:
    services.add(1, "One")
    manager = make_manager(settings)
    run_id = await started(manager, services)
    services.tasks_error = httpx.ConnectError("refused")
    await finish(manager, run_id, pr_url=PR, pr_number=42)
    assert manager.store.get_run(run_id)["status"] == "paused"  # type: ignore[index]
    [event] = services.events
    assert "not reachable" in event["text"] and PR in event["text"]


async def test_a_bug_in_the_step_never_reaches_the_report(settings, services, monkeypatch) -> None:
    services.add(1, "One")
    manager = make_manager(settings)
    run_id = await started(manager, services)

    async def broken(self, run, task):
        raise RuntimeError("bug")

    monkeypatch.setattr(runs.Runs, "_step", broken)
    task = current(manager, run_id)
    result = await manager.record_report(task["task_id"], Report(status="success"))
    assert result is not None and result["status"] == "success"
    assert manager.store.get_run(run_id)["status"] == "paused"  # type: ignore[index]
    assert "ended: success" in services.events[-1]["text"]


async def test_a_task_of_a_missing_run_sends_the_report(settings, services) -> None:
    manager = make_manager(settings)
    task = (await manager.develop("alex", REPO, "x", branch="f", run_id="gone"))["task_id"]
    await manager.record_report(task, Report(status="success"))
    assert "ended: success" in services.events[0]["text"]


async def test_the_events_go_nowhere_without_a_destination(settings, services) -> None:
    services.add(1, "One")
    manager = make_manager(settings)
    run_id = await started(manager, services)
    manager.store.update_run(run_id, status="active")
    run = manager.store.get_run(run_id)
    assert run is not None
    # Mia has no chat: the event is not sent, and nothing raises.
    manager.store.create_run(run_id="r2", person="mia", repo=REPO, project="p", branch="b")
    await manager.runs._say(manager.store.get_run("r2"), "text", None)  # type: ignore[arg-type]
    assert services.events == []


# --- pause, resume, stop, status -------------------------------------------


async def test_pause_resume_and_stop(settings, services) -> None:
    services.add(1, "One")
    services.add(2, "Two")
    manager = make_manager(settings)
    run_id = await started(manager, services)
    get = lambda: manager.store.get_run(run_id)  # noqa: E731
    paused = await manager.runs.pause(get())  # type: ignore[arg-type]
    assert paused["status"] == "paused"
    assert (await manager.runs.pause(get()))["message"].startswith("The run is paused already")  # type: ignore[arg-type]
    # The task still runs: resume only makes the run active.
    resumed = await manager.runs.resume(get())  # type: ignore[arg-type]
    assert resumed["status"] == "active" and "goes on" in resumed["message"]
    stopped = await manager.runs.stop(get())  # type: ignore[arg-type]
    assert stopped["status"] == "stopped"
    assert (await manager.runs.stop(get()))["message"].endswith("already.")  # type: ignore[arg-type]
    assert (await manager.runs.pause(get()))["reason"] == "run_not_pausable"  # type: ignore[arg-type]
    assert (await manager.runs.resume(get()))["reason"] == "run_not_resumable"  # type: ignore[arg-type]


async def test_resume_dispatches_a_stalled_run_and_ends_an_empty_one(settings, services) -> None:
    services.add(1, "One")
    manager = make_manager(settings)
    run_id = await started(manager, services)
    task = current(manager, run_id)
    # A restart ended the task and sent no step: the run is active with no task.
    manager.store.update_task(task["task_id"], status="failed")
    manager.locks.release(REPO, task["scope"], task["task_id"])
    result = await manager.runs.resume(manager.store.get_run(run_id))  # type: ignore[arg-type]
    assert "Task #1 One started" in result["message"]
    assert current(manager, run_id)["task_id"] != task["task_id"]
    # Nothing is open any more.
    services.by_number(1)["done"] = True
    manager.store.update_run(run_id, status="paused")
    manager.store.update_task(current(manager, run_id)["task_id"], status="success")
    result = await manager.runs.resume(manager.store.get_run(run_id))  # type: ignore[arg-type]
    assert result["status"] == "done"


async def test_resume_with_blocked_tasks_stays_paused(settings, services) -> None:
    services.add(1, "One")
    manager = make_manager(settings)
    run_id = await started(manager, services)
    task = current(manager, run_id)
    manager.store.update_task(task["task_id"], status="failed")
    services.by_number(1)["blocked"] = True
    manager.store.update_run(run_id, status="paused")
    result = await manager.runs.resume(manager.store.get_run(run_id))  # type: ignore[arg-type]
    assert result["status"] == "paused" and "blocked" in result["message"]


async def test_resume_reports_a_refused_dispatch_and_a_missing_tasks_addon(
    settings, services
) -> None:
    services.add(1, "One")
    manager = make_manager(settings)
    run_id = await started(manager, services)
    task = current(manager, run_id)
    manager.store.update_task(task["task_id"], status="failed")
    manager.locks.release(REPO, task["scope"], task["task_id"])
    await manager.develop("alex", REPO, "other", branch="feat-chat")
    manager.store.update_run(run_id, status="paused")
    run = manager.store.get_run(run_id)
    result = await manager.runs.resume(run)  # type: ignore[arg-type]
    assert result["reason"] == "locked"
    assert manager.store.get_run(run_id)["status"] == "paused"  # type: ignore[index]
    services.tasks_error = httpx.ConnectError("refused")
    assert (await manager.runs.resume(run))["reason"] == "tasks_unreachable"  # type: ignore[arg-type]
    manager.settings = make_settings(settings.data_dir)
    assert (await manager.runs.resume(run))["reason"] == "tasks_not_configured"  # type: ignore[arg-type]


async def test_find_by_id_name_and_branch(settings, services) -> None:
    services.add(1, "One")
    manager = make_manager(settings)
    run_id = await started(manager, services)
    find = manager.runs.find
    assert find("alex", run_id, None)["run_id"] == run_id
    assert find("alex", None, "chat project")["run_id"] == run_id
    assert find("alex", None, "chat")["run_id"] == run_id  # the branch feat-chat
    assert find(None, None, services.project_id)["run_id"] == run_id
    assert find("mia", run_id, None)["reason"] == "run_not_found"
    assert find("alex", "nope", None)["reason"] == "run_not_found"
    assert find("alex", None, "other")["reason"] == "run_not_found"
    assert find("alex", None, None)["reason"] == "invalid_arguments"
    manager.store.create_run(
        run_id="r2",
        person="alex",
        repo="github.com/example-home/b",
        project=services.project_id,
        project_name="Chat Project",
        branch="feat-chat",
    )
    assert find("alex", None, "Chat Project")["reason"] == "run_ambiguous"


# --- the tools over MCP ----------------------------------------------------


async def test_the_run_tools(settings, services) -> None:
    services.add(1, "One")
    services.add(2, "Two")
    app = build_app(settings, stub_delay_s=None)
    async with mcp_session(app) as session:
        started_run = payload(
            await session.call_tool("run_project", {"repo": REPO, "project": "chat"})
        )
        run_id = started_run["run_id"]
        status = payload(await session.call_tool("run_status", {"run_id": run_id}))
        assert status["status"] == "active" and status["current_task"]["status"] == "running"
        listed = payload(await session.call_tool("run_status", {}))
        assert [r["run_id"] for r in listed["runs"]] == [run_id]
        paused = payload(await session.call_tool("pause_run", {"project": "chat"}))
        assert paused["status"] == "paused"
        resumed = payload(await session.call_tool("resume_run", {"run_id": run_id}))
        assert resumed["status"] == "active"
        refused = payload(await session.call_tool("run_status", {"run_id": "nope"}))
        assert refused["reason"] == "run_not_found"
        refused = payload(await session.call_tool("stop_run", {"run_id": "nope"}))
        assert refused["reason"] == "run_not_found"
        refused = payload(await session.call_tool("pause_run", {"run_id": "nope"}))
        assert refused["reason"] == "run_not_found"
        refused = payload(await session.call_tool("resume_run", {"run_id": "nope"}))
        assert refused["reason"] == "run_not_found"
        stopped = payload(await session.call_tool("stop_run", {"project": "chat"}))
        assert stopped["status"] == "stopped"


async def test_another_person_cannot_see_or_steer_a_run(settings, services) -> None:
    services.add(1, "One")
    app = build_app(settings, stub_delay_s=None)
    async with mcp_sessions(app, "alex-token", MIA, ADDON) as (alex, mia, addon):
        run_id = payload(await alex.call_tool("run_project", {"repo": REPO, "project": "chat"}))[
            "run_id"
        ]
        for tool in ("run_status", "pause_run", "resume_run", "stop_run"):
            result = payload(await mia.call_tool(tool, {"run_id": run_id}))
            assert result["reason"] == "run_not_found"
        assert payload(await mia.call_tool("run_status", {}))["runs"] == []
        # The addon token reads every run, and steers none.
        seen = payload(await addon.call_tool("run_status", {"run_id": run_id}))
        assert seen["run_id"] == run_id
        assert len(payload(await addon.call_tool("run_status", {}))["runs"]) == 1
        assert error(await addon.call_tool("stop_run", {"run_id": run_id})).endswith(
            "403 forbidden: this tool needs a person"
        )
        assert error(
            await addon.call_tool("run_project", {"repo": REPO, "project": "chat"})
        ).endswith("403 forbidden: this tool needs a person")
        still = payload(await alex.call_tool("run_status", {"run_id": run_id}))
        assert still["status"] == "active"


async def test_a_stamped_call_gets_the_pull_request_link(settings, services) -> None:
    services.add(1, "One")
    services.add(2, "Two")
    app = build_app(settings, stub_delay_s=None)
    meta = {WORKSPACE_META_KEY: WORKSPACE}
    async with mcp_session(app) as session:
        result = await session.call_tool(
            "run_project", {"repo": REPO, "project": "chat"}, meta=meta
        )
        # The pull request does not exist yet: the result has no link.
        assert [b.type for b in result.content] == ["text"]
        run_id = payload(result)["run_id"]
        from joshua_developer import server

        manager = server._get_manager()
        manager.store.update_run(run_id, pr_url=PR, pr_number=42)
        stamped = await session.call_tool("run_status", {"run_id": run_id}, meta=meta)
        assert [b.type for b in stamped.content] == ["text", "resource_link"]
        assert str(stamped.content[1].uri) == PR
        plain = await session.call_tool("run_status", {"run_id": run_id})
        assert [b.type for b in plain.content] == ["text"]
        assert json.loads(plain.content[0].text)["pr_url"] == PR
        task = manager.store.get_task_full(manager.store.get_run(run_id)["current_task_id"])
        assert task["workspace"] == WORKSPACE


# --- helpers ---------------------------------------------------------------


def test_brief_of_leaves_out_the_lines_of_the_run() -> None:
    task = {
        "title": " Title ",
        "notes": "Keep this.\ndispatched as developer task t on b\n  done in https://x\nAnd this.",
    }
    assert brief_of(task) == "Title\n\nKeep this.\nAnd this.\n\nCommit and push your work."
    assert brief_of({"title": "T", "notes": ""}) == "T\n\nCommit and push your work."
    assert brief_of({"title": "T", "notes": None}) == "T\n\nCommit and push your work."
    assert not re.search("dispatched", brief_of(task))


def test_append_note() -> None:
    assert append_note(None, "x") == "x"
    assert append_note("  ", "x") == "x"
    assert append_note("a\n", "x") == "a\nx"


@pytest.mark.parametrize(
    "url", ["http://tasks:8000", "http://tasks:8000/", "http://tasks:8000/api"]
)
async def test_the_tasks_url_may_end_in_api(services, url) -> None:
    services.add(1, "One")
    client = TasksClient(url)
    assert client.base == "http://tasks:8000/api"
    assert (await client.by_number(1))["title"] == "One"


async def test_the_tasks_client_names_its_failures(services) -> None:
    client = TasksClient("http://tasks:8000")
    with pytest.raises(TasksError) as missing:
        await client.next_step("nope")
    assert missing.value.status == 404
    services.tasks_error = httpx.ConnectError("refused")
    with pytest.raises(TasksError, match="not reachable"):
        await client.by_number(1)
    assert await client.group_name(services.project_id, G1) is None
    services.tasks_error = None
    assert await client.group_name(services.project_id, G1) == "Phase one"
    assert await client.group_name(services.project_id, str(uuid.uuid4())) is None
    assert await client.group_name(services.project_id, None) is None


async def test_the_tasks_client_refuses_an_answer_that_is_not_json(monkeypatch) -> None:
    monkeypatch.setattr(
        runs,
        "make_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>"))
        ),
    )
    with pytest.raises(TasksError, match="no JSON"):
        await TasksClient("http://tasks:8000").by_number(1)


async def test_an_empty_next_steps_answer_is_a_missing_project(monkeypatch) -> None:
    monkeypatch.setattr(
        runs,
        "make_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
        ),
    )
    with pytest.raises(TasksError) as caught:
        await TasksClient("http://tasks:8000").next_step("x")
    assert caught.value.status == 404
