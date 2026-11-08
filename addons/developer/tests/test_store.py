"""The SQLite task store and the person settings."""

from __future__ import annotations

from pathlib import Path

import pytest
from joshua_developer.store import (
    ALLOWED_UPDATE_FIELDS,
    STATUS_COLUMNS,
    TaskStore,
    build_update_sql,
)


@pytest.fixture
def store(tmp_path: Path):
    store = TaskStore(tmp_path / "data" / "developer.db")
    yield store
    store.close()


def add(store: TaskStore, task_id: str, person: str = "alex", **extra) -> None:
    fields = {
        "task_id": task_id,
        "person": person,
        "task_type": "develop",
        "repo": "github.com/example-home/app",
        "scope": f"branch:{task_id}",
        "persona": "opus",
        "brief": "the brief",
    }
    fields.update(extra)
    store.create_task(**fields)


def test_build_update_sql() -> None:
    sql, params = build_update_sql("task-1", {"status": "success", "pr_number": 9})
    assert sql == "UPDATE tasks SET status = ?, pr_number = ? WHERE task_id = ?"
    assert params == ["success", 9, "task-1"]


def test_build_update_sql_serializes_a_report() -> None:
    _, params = build_update_sql("t", {"report": {"summary": "x"}})
    assert params[0] == '{"summary": "x"}'


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        ({"status": "success", "evil; DROP TABLE": 1}, "unknown update fields"),
        ({}, "no fields"),
        ({"status": "in_progress"}, "unknown status"),
        ({"person": "mia"}, "unknown update fields"),
    ],
)
def test_build_update_sql_refuses(fields: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        build_update_sql("task-1", fields)


def test_the_report_fields_are_allowed() -> None:
    report_fields = {
        "status",
        "branch_name",
        "commit_hash",
        "summary",
        "report",
        "completed_at",
        "pr_url",
        "pr_number",
        "input_tokens",
        "output_tokens",
        "estimated_cost",
        "error",
        "open_question",
        "answer",
    }
    assert report_fields <= ALLOWED_UPDATE_FIELDS


def test_create_get_and_update(store: TaskStore) -> None:
    add(store, "t1", notify="telegram:dm:alex", branch_name="feature")
    task = store.get_task("t1")
    assert task is not None
    assert tuple(task) == (*STATUS_COLUMNS, "waiting_since")
    assert task["paused_s"] == 0
    assert task["asked_at"] is None and task["waiting_since"] is None
    assert task["status"] == "dispatched"
    assert task["notify"] == "telegram:dm:alex"
    assert "brief" not in task
    store.update_task("t1", status="success", report={"summary": "done", "files_changed": ["a"]})
    full = store.get_task_full("t1")
    assert full is not None
    assert full["brief"] == "the brief"
    assert full["report"] == {"summary": "done", "files_changed": ["a"]}
    assert store.get_task("missing") is None
    assert store.get_task_full("missing") is None


def test_an_unknown_task_type_is_refused(store: TaskStore) -> None:
    with pytest.raises(ValueError, match="unknown task type"):
        add(store, "t1", task_type="deploy")


def test_list_tasks_is_newest_first_and_per_person(store: TaskStore) -> None:
    add(store, "a1")
    add(store, "m1", person="mia")
    add(store, "a2")
    assert [t["task_id"] for t in store.list_tasks("alex")] == ["a2", "a1"]
    assert [t["task_id"] for t in store.list_tasks("mia")] == ["m1"]
    assert [t["task_id"] for t in store.list_tasks(None, limit=2)] == ["a2", "m1"]


def test_fail_active_tasks(store: TaskStore) -> None:
    add(store, "d")
    add(store, "r")
    add(store, "s")
    store.update_task("r", status="running")
    store.update_task("s", status="success")
    changed = store.fail_active_tasks("manager restarted")
    assert sorted(t["task_id"] for t in changed) == ["d", "r"]
    for task_id in ("d", "r"):
        task = store.get_task(task_id)
        assert task is not None
        assert task["status"] == "failed"
        assert task["error"] == "manager restarted"
        assert task["completed_at"]
    assert store.get_task("s")["status"] == "success"  # type: ignore[index]
    assert store.list_active_tasks() == []


def test_settings_survive_a_reopen(tmp_path: Path) -> None:
    path = tmp_path / "developer.db"
    store = TaskStore(path)
    assert store.get_settings("alex") == {
        "git_name": None,
        "git_email": None,
        "default_persona": None,
        "notify": None,
    }
    store.set_settings("alex", git_name="Alex", default_persona="fable")
    store.set_settings("alex", notify="telegram:dm:alex", git_name=None)
    store.close()

    reopened = TaskStore(path)
    assert reopened.get_settings("alex") == {
        "git_name": "Alex",
        "git_email": None,
        "default_persona": "fable",
        "notify": "telegram:dm:alex",
    }
    reopened.set_settings("alex", default_persona="")
    assert reopened.get_settings("alex")["default_persona"] is None
    with pytest.raises(ValueError, match="unknown settings fields"):
        reopened.set_settings("alex", token="x")
    reopened.close()


def test_worker_tokens_are_found_and_cleared_when_the_task_ends(store: TaskStore) -> None:
    add(store, "a", worker_token="token-a")
    add(store, "b", worker_token="token-b")
    add(store, "c")
    assert store.find_task_by_worker_token("token-a") == "a"
    assert store.find_task_by_worker_token("token-b") == "b"
    assert store.find_task_by_worker_token("token-c") is None
    assert store.find_task_by_worker_token("") is None
    assert "worker_token" not in (store.get_task("a") or {})
    store.update_task("a", status="success")
    assert store.clear_ended_worker_tokens() == 1
    assert store.find_task_by_worker_token("token-a") is None
    assert store.find_task_by_worker_token("token-b") == "b"


def test_an_old_database_gets_the_new_columns(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "developer.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, person TEXT NOT NULL, "
        "task_type TEXT NOT NULL, repo TEXT NOT NULL, scope TEXT NOT NULL, "
        "branch_name TEXT, base_branch TEXT, persona TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'dispatched', brief TEXT NOT NULL DEFAULT '', "
        "instructions TEXT, pr_url TEXT, pr_number INTEGER, commit_hash TEXT, summary TEXT, "
        "report TEXT, error TEXT, open_question TEXT, answer TEXT, input_tokens INTEGER, "
        "output_tokens INTEGER, estimated_cost REAL, notify TEXT, created_at TEXT NOT NULL, "
        "started_at TEXT, completed_at TEXT)"
    )
    conn.commit()
    conn.close()
    store = TaskStore(path)
    add(store, "a", worker_token="t")
    assert store.append_session_log("a", "line\n")
    full = store.get_task_full("a")
    assert full is not None
    assert full["worker_token"] == "t"
    assert full["session_log"] == "line\n"
    assert full["paused_s"] == 0
    assert full["asked_at"] is None and full["last_wait_s"] is None
    store.close()


def test_end_wait_adds_the_wait_to_paused_s(store: TaskStore) -> None:
    from datetime import UTC, datetime, timedelta

    start = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    add(store, "a")
    assert store.end_wait("a", start) is None
    assert store.end_wait("missing", start) is None
    store.update_task("a", asked_at=start.isoformat(), paused_s=30)
    task = store.get_task("a")
    assert task is not None and task["waiting_since"] == start.isoformat()
    assert store.end_wait("a", start + timedelta(seconds=90.4)) == 90
    full = store.get_task_full("a")
    assert full is not None
    assert (full["paused_s"], full["asked_at"], full["last_wait_s"]) == (120, None, 90)


def test_a_restart_clears_the_open_wait(store: TaskStore) -> None:
    add(store, "a")
    store.update_task("a", status="running", asked_at="2026-10-04T12:00:00+00:00")
    store.fail_active_tasks("manager restarted")
    task = store.get_task("a")
    assert task is not None and task["asked_at"] is None and task["waiting_since"] is None
