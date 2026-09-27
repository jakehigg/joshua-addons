"""The manager rules that the tools do not reach: a runtime failure, a late report,
the reports after a restart, and the log that never holds a brief."""

from __future__ import annotations

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


async def test_a_report_with_a_pull_request_and_a_question(settings) -> None:
    manager = make_manager(settings, delay_s=None)
    task_id = (await manager.develop("alex", REPO, "brief", branch="feature"))["task_id"]
    manager.mark_running(task_id)  # a second mark changes nothing
    report = Report(
        status="blocked",
        summary="needs a decision",
        open_question="Which port?",
        branch="feature-2",
        pr_url="https://github.com/example-home/app/pull/9",
        pr_number=9,
        input_tokens=10,
        output_tokens=20,
        estimated_cost=0.5,
    )
    task = await manager.record_report(task_id, report)
    assert task is not None
    assert task["status"] == "blocked"
    assert task["open_question"] == "Which port?"
    assert task["branch_name"] == "feature-2"
    assert task["pr_number"] == 9
    assert task["estimated_cost"] == 0.5
    assert manager.locks.get(REPO, "branch:feature") is None
    manager.mark_running("no-such-task")


async def test_the_lock_outlives_the_persona_timeout(settings) -> None:
    manager = make_manager(settings, delay_s=None)
    await manager.develop("alex", REPO, "brief", branch="feature", persona="sonnet")
    info = manager.locks.get(REPO, "branch:feature")
    assert info is not None
    from datetime import datetime

    span = datetime.fromisoformat(info.expires_at) - datetime.fromisoformat(info.started_at)
    assert span.total_seconds() == 1200 + 600


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
        manager.answer("alex", {"task_id": result["task_id"]}, "SECRET-ANSWER-TEXT")
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
