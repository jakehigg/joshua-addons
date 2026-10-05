"""The manager rules that the tools do not reach: a runtime failure, a late report,
the reports after a restart, and the log that never holds a brief."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from conftest import make_settings
from joshua_developer import notify, server
from joshua_developer.locks import LockManager
from joshua_developer.manager import RESTART_ERROR, Manager
from joshua_developer.runtime import Report, StubRuntime
from joshua_developer.store import TaskStore

REPO = "github.com/example-home/app"


_opened: list[TaskStore] = []


@pytest.fixture(autouse=True)
def close_stores():
    yield
    while _opened:
        _opened.pop().close()


def make_store(settings) -> TaskStore:
    store = TaskStore(settings.db_path)
    _opened.append(store)
    return store


def make_manager(settings, delay_s: float | None = 0) -> Manager:
    manager = Manager(settings, make_store(settings), LockManager())
    manager.runtime = StubRuntime(manager, delay_s=delay_s)
    return manager


class BrokenRuntime:
    def count_active(self) -> int:
        return 0

    async def start(self, task: dict) -> str:
        raise OSError("socket proxy said no to token-in-error")


async def test_a_runtime_that_cannot_start_fails_the_task_and_frees_the_lock(settings) -> None:
    manager = make_manager(settings)
    manager.runtime = BrokenRuntime()
    result = await manager.develop("alex", REPO, "brief", branch="feature")
    assert result["status"] == "failed"
    assert "token-in-error" not in json.dumps(result)
    task = manager.store.get_task(result["task_id"])
    assert task is not None
    assert task["status"] == "failed"
    assert task["error"] == "the worker did not start: OSError"
    assert manager.locks.get(REPO, "branch:feature") is None


class SlowRuntime:
    """A runtime whose start takes time, and that counts a worker only once it runs."""

    def __init__(self) -> None:
        self.started: list[str] = []

    def count_active(self) -> int:
        return len(self.started)

    async def start(self, task: dict) -> str:
        await asyncio.sleep(0.05)
        self.started.append(task["task_id"])
        return task["task_id"]


async def test_two_dispatches_at_once_cannot_both_take_the_last_slot(data_dir) -> None:
    settings = make_settings(data_dir, config={"max_workers": 1})
    manager = Manager(settings, make_store(settings), LockManager())
    runtime = SlowRuntime()
    manager.runtime = runtime
    results = await asyncio.gather(
        manager.develop("alex", REPO, "brief one", branch="one"),
        manager.develop("alex", "github.com/example-home/other", "brief two", branch="two"),
    )
    statuses = sorted(result["status"] for result in results)
    assert statuses == ["dispatched", "rejected"]
    [refused] = [result for result in results if result["status"] == "rejected"]
    assert refused["reason"] == "concurrency_limit"
    assert len(runtime.started) == 1
    assert manager.store.count_active_tasks() == 1


async def test_a_task_whose_worker_still_starts_takes_its_slot(data_dir) -> None:
    settings = make_settings(data_dir, config={"max_workers": 1})
    manager = Manager(settings, make_store(settings), LockManager())
    # The worker never reads its brief, and the runtime counts no container.
    manager.runtime = BrokenRuntime()
    manager.runtime.start = SlowRuntime().start  # type: ignore[method-assign]
    first = await manager.develop("alex", REPO, "brief", branch="one")
    assert manager.store.get_task(first["task_id"])["status"] == "dispatched"  # type: ignore[index]
    second = await manager.develop("alex", REPO, "brief", branch="two")
    assert second["reason"] == "concurrency_limit"


async def test_a_manager_without_a_runtime_refuses_to_dispatch(settings) -> None:
    manager = Manager(settings, make_store(settings), LockManager())
    try:
        await manager.develop("alex", REPO, "brief")
    except RuntimeError as exc:
        assert "no runtime" in str(exc)
    else:  # pragma: no cover - the call must raise
        raise AssertionError("develop did not raise")


async def test_a_report_for_an_ended_or_unknown_task_is_ignored(settings) -> None:
    manager = make_manager(settings)
    task_id = (await manager.develop("alex", REPO, "brief"))["task_id"]
    late = Report(status="failed", summary="late")
    task = await manager.record_report(task_id, late)
    assert task is not None and task["status"] == "success"
    assert await manager.record_report("no-such-task", late) is None


async def test_a_report_with_a_question(settings) -> None:
    manager = make_manager(settings, delay_s=None)
    task_id = (await manager.develop("alex", REPO, "brief", branch="feature"))["task_id"]
    manager.mark_running(task_id)  # a second mark changes nothing
    report = Report(
        status="blocked",
        summary="needs a decision",
        open_question="Which port?",
        input_tokens=10,
        output_tokens=20,
        estimated_cost=0.5,
    )
    task = await manager.record_report(task_id, report)
    assert task is not None
    assert task["status"] == "blocked"
    assert task["open_question"] == "Which port?"
    # The branch is the task row's own.
    assert task["branch_name"] == "feature"
    assert task["pr_number"] is None
    assert task["estimated_cost"] == 0.5
    assert manager.locks.get(REPO, "branch:feature") is None
    manager.mark_running("no-such-task")


async def test_the_lock_stays_until_the_task_ends(settings) -> None:
    from datetime import UTC, datetime, timedelta

    now = [datetime(2026, 10, 4, 12, 0, tzinfo=UTC)]
    locks = LockManager(clock=lambda: now[0])
    manager = Manager(settings, make_store(settings), locks)
    manager.runtime = StubRuntime(manager, delay_s=None)
    task_id = (await manager.develop("alex", REPO, "brief", branch="feature"))["task_id"]
    # Far past the persona timeout plus the old margin: the lock stays.
    now[0] += timedelta(days=1)
    again = await manager.develop("alex", REPO, "other", branch="feature")
    assert again["status"] == "rejected" and again["reason"] == "locked"
    assert manager.locks.get(REPO, "branch:feature").task_id == task_id  # type: ignore[union-attr]
    await manager.record_report(task_id, Report(status="timed_out"))
    assert manager.locks.get(REPO, "branch:feature") is None


async def test_a_stored_persona_that_is_gone_falls_back(settings) -> None:
    manager = make_manager(settings)
    manager.store.set_settings("alex", default_persona="retired")
    assert manager.effective_settings("alex")["default_persona"] == "sonnet"
    assert manager.effective_settings("zoe")["default_persona"] == "opus"
    assert manager.resolve_persona(None, None) == "opus"
    assert manager.resolve_persona(None, "fable") == "fable"


async def test_recover_sends_one_report_for_each_failed_task(settings, monkeypatch) -> None:
    first = make_manager(settings, delay_s=None)
    task_id = (await first.develop("alex", REPO, "brief"))["task_id"]
    first.store.close()

    sent: list[dict] = []

    async def fake_send(settings, task, default=None) -> bool:
        sent.append(task)
        return True

    monkeypatch.setattr(notify, "send_report", fake_send)
    second = make_manager(settings)
    assert second.recover() == [task_id]
    await second.send_recovered_reports()
    await second.send_recovered_reports()
    assert [task["task_id"] for task in sent] == [task_id]
    assert sent[0]["error"] == RESTART_ERROR


async def test_the_app_lifespan_sends_the_recovered_reports(settings, monkeypatch) -> None:
    first = make_manager(settings, delay_s=None)
    await first.develop("alex", REPO, "brief")
    first.store.close()

    sent: list[str] = []

    async def fake_send(settings, task, default=None) -> bool:
        sent.append(task["task_id"])
        return True

    monkeypatch.setattr(notify, "send_report", fake_send)
    app = server.build_app(settings, stub_delay_s=0)
    inner = app.app  # type: ignore[attr-defined]
    async with inner.router.lifespan_context(inner):
        pass
    assert len(sent) == 1
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        assert (await client.get("/healthz")).json() == {"ok": True}


async def test_the_log_never_holds_the_brief(settings, caplog) -> None:
    manager = make_manager(settings)
    with caplog.at_level(logging.DEBUG):
        result = await manager.develop("alex", REPO, "SECRET-BRIEF-TEXT")
        manager.store.update_task(result["task_id"], status="running", open_question="q?")
        await manager.answer("alex", {"task_id": result["task_id"]}, "SECRET-ANSWER-TEXT")
    text = " ".join(str(record.msg) for record in caplog.records)
    assert "task dispatched" in text
    assert "SECRET-BRIEF-TEXT" not in text
    assert "SECRET-ANSWER-TEXT" not in text


async def test_build_app_reads_the_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DEVELOPER_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("DEVELOPER_DATA_DIR", str(tmp_path / "data"))
    for name in ("DEVELOPER_TOKENS", "ADDON_TOKEN", "CHANNELS_URL", "WORKER_RUNTIME"):
        monkeypatch.delenv(name, raising=False)
    server.build_app()
    assert (tmp_path / "data" / "developer.db").is_file()
    assert server._get_manager().settings.open is True


def test_make_settings_is_a_fixture_helper(tmp_path) -> None:
    assert make_settings(tmp_path).db_path == tmp_path / "developer.db"


@pytest.mark.parametrize("port", ["99999", "0"])
async def test_a_repo_with_a_bad_port_is_invalid_arguments(settings, port: str) -> None:
    manager = make_manager(settings)
    repo = f"https://user:ghp_SECRETSECRET@github.com:{port}/example-home/app"
    result = await manager.develop("alex", repo, "brief")
    assert result["status"] == "rejected"
    assert result["reason"] == "invalid_arguments"
    assert "ghp_SECRETSECRET" not in json.dumps(result)
    rework = await manager.rework("alex", repo, 5, "fix")
    assert rework["reason"] == "invalid_arguments"
