"""The bundle of a failed push: stored, served, and resumed from."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from joshua_developer.locks import LockManager
from joshua_developer.manager import BUNDLE_KEEP_DAYS, Manager
from joshua_developer.runtime import Report, StubRuntime
from joshua_developer.store import TaskStore
from test_worker_api import dispatch, worker_client


@pytest.fixture
def manager(settings):
    store = TaskStore(settings.db_path)
    manager = Manager(settings, store, LockManager())
    manager.runtime = StubRuntime(manager, delay_s=None)
    yield manager
    store.close()


@pytest.fixture
def bundle_bytes(tmp_path: Path) -> bytes:
    """A real git bundle of one commit on ``feature``."""
    repo = tmp_path / "src"

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=Seed", "-c", "user.email=seed@example.test", *args],
            cwd=repo,
            check=True,
            capture_output=True,
        )

    repo.mkdir()
    git("init", "-q", "-b", "feature")
    (repo / "a.txt").write_text("a\n")
    git("add", "a.txt")
    git("commit", "-q", "-m", "a")
    out = tmp_path / "work.bundle"
    git("bundle", "create", "-q", str(out), "feature")
    return out.read_bytes()


def push_failed(*, saved: bool = True, unpushed: str = "success") -> Report:
    return Report(
        status="failed",
        summary="Added the search tool.",
        files_changed=["search.py"],
        tests_run=["pytest: 12 passed"],
        clone_head="b" * 40,
        commit_hash="a" * 40,
        unpushed_status=unpushed,  # type: ignore[arg-type]
        bundle_saved=saved,
        error="the push of feature failed: 403",
    )


async def finish(manager: Manager, task_id: str, report: Report) -> None:
    await manager.record_report(task_id, report)
    manager.runtime._active.discard(task_id)  # type: ignore[union-attr]


async def test_a_worker_stores_and_reads_its_bundle(manager, bundle_bytes) -> None:
    task_id, token = await dispatch(manager, branch="feature")
    async with worker_client(manager, token) as client:
        saved = await client.put("/worker/bundle", content=bundle_bytes)
        assert saved.status_code == 200
        assert saved.json() == {"saved": True, "bytes": len(bundle_bytes)}
        served = await client.get("/worker/bundle")
    assert served.status_code == 200
    assert served.content == bundle_bytes
    assert manager.bundle_path(task_id).read_bytes() == bundle_bytes
    assert not list(manager.bundle_dir.glob("*.part"))


async def test_a_task_with_no_bundle_gets_404(manager) -> None:
    _, token = await dispatch(manager)
    async with worker_client(manager, token) as client:
        response = await client.get("/worker/bundle")
    assert response.status_code == 404
    assert response.json() == {"error": "no_bundle"}


async def test_a_body_that_is_not_a_bundle_is_refused(manager) -> None:
    task_id, token = await dispatch(manager)
    async with worker_client(manager, token) as client:
        response = await client.put("/worker/bundle", content=b"#!/bin/sh\nrm -rf /\n")
    assert response.status_code == 400
    assert response.json() == {"error": "not_a_bundle"}
    assert not manager.has_bundle(task_id)
    assert not list(manager.bundle_dir.iterdir())


async def test_a_bundle_over_the_limit_is_refused(manager, monkeypatch, bundle_bytes) -> None:
    from joshua_developer import worker_api

    monkeypatch.setattr(worker_api, "MAX_BUNDLE", 64)
    task_id, token = await dispatch(manager)

    async def chunks():
        yield bundle_bytes[:40]
        yield bundle_bytes[40:] + b"x" * 64

    async with worker_client(manager, token) as client:
        declared = await client.put("/worker/bundle", content=b"x" * 65)
        streamed = await client.put("/worker/bundle", content=chunks())
    assert declared.status_code == 413
    assert streamed.status_code == 413
    assert not manager.has_bundle(task_id)
    assert not list(manager.bundle_dir.iterdir())


async def test_an_answer_resumes_a_failed_task_from_its_bundle(manager, bundle_bytes) -> None:
    task_id, token = await dispatch(manager, branch="feature")
    async with worker_client(manager, token) as client:
        await client.put("/worker/bundle", content=bundle_bytes)
    await finish(manager, task_id, push_failed())
    assert manager.store.get_task_full(task_id)["status"] == "failed"

    result = await manager.answer("alex", {"task_id": task_id}, "The token is fixed; push it.")
    assert result["status"] == "dispatched" and result["resumed"] is True

    full = manager.store.get_task_full(task_id)
    async with worker_client(manager, full["worker_token"]) as client:
        brief = (await client.get("/worker/brief")).json()
        served = await client.get("/worker/bundle")
    assert brief["resumed"] is True
    assert brief["bundle"] is True
    assert brief["push_only"] is True
    assert brief["saved"] == {
        "summary": "Added the search tool.",
        "files_changed": ["search.py"],
        "tests_run": ["pytest: 12 passed"],
    }
    assert served.content == bundle_bytes


async def test_a_blocked_session_resumes_with_a_session(manager, bundle_bytes) -> None:
    task_id, token = await dispatch(manager, branch="feature")
    async with worker_client(manager, token) as client:
        await client.put("/worker/bundle", content=bundle_bytes)
    await finish(manager, task_id, push_failed(unpushed="blocked"))
    await manager.answer("alex", {"task_id": task_id}, "Port 8080.")
    full = manager.store.get_task_full(task_id)
    async with worker_client(manager, full["worker_token"]) as client:
        brief = (await client.get("/worker/brief")).json()
    assert brief["bundle"] is True
    assert brief["push_only"] is False and brief["saved"] is None
    assert brief["answer"] == "Port 8080."


async def test_a_failed_task_with_no_bundle_is_not_resumable(manager) -> None:
    task_id, _ = await dispatch(manager, branch="feature")
    await finish(manager, task_id, push_failed(saved=False))
    result = await manager.answer("alex", {"task_id": task_id}, "push it")
    assert result["status"] == "rejected"
    assert result["reason"] == "not_waiting"


async def test_a_blocked_task_with_only_a_bundle_is_resumable(manager, bundle_bytes) -> None:
    """A task that never pushed is resumable when it has a bundle."""
    task_id, token = await dispatch(manager, branch="feature")
    async with worker_client(manager, token) as client:
        await client.put("/worker/bundle", content=bundle_bytes)
    report = Report(status="blocked", open_question="Which port?", commit_hash="a" * 40)
    await finish(manager, task_id, report)
    result = await manager.answer("alex", {"task_id": task_id}, "8080")
    assert result["status"] == "dispatched"


async def test_a_blocked_task_with_no_push_and_no_bundle_is_not_resumable(manager) -> None:
    task_id, _ = await dispatch(manager, branch="feature")
    await finish(manager, task_id, Report(status="blocked", open_question="Which port?"))
    result = await manager.answer("alex", {"task_id": task_id}, "8080")
    assert result["reason"] == "not_resumable"
    assert "saved no commits" in result["message"]


async def test_a_push_deletes_the_bundle(manager, git_host, bundle_bytes) -> None:
    task_id, token = await dispatch(manager, branch="feature")
    async with worker_client(manager, token) as client:
        await client.put("/worker/bundle", content=bundle_bytes)
    await finish(manager, task_id, push_failed())
    await manager.answer("alex", {"task_id": task_id}, "push it")
    assert manager.has_bundle(task_id)
    pushed = Report(
        status="success",
        summary="Added the search tool.",
        pushed=True,
        head="feature",
        clone_head="b" * 40,
        commit_hash="a" * 40,
    )
    await finish(manager, task_id, pushed)
    assert not manager.has_bundle(task_id)
    assert manager.store.get_task_full(task_id)["status"] == "success"


async def test_old_bundles_are_pruned_at_start(manager, bundle_bytes) -> None:
    manager.bundle_dir.mkdir(parents=True)
    old = manager.bundle_dir / "old.bundle"
    new = manager.bundle_dir / "new.bundle"
    old.write_bytes(bundle_bytes)
    new.write_bytes(bundle_bytes)
    stale = old.stat().st_mtime - (BUNDLE_KEEP_DAYS + 1) * 86400
    os.utime(old, (stale, stale))
    manager.recover()
    assert not old.exists() and new.exists()


def test_prune_with_no_bundle_dir(manager) -> None:
    assert manager.prune_bundles() == 0


def test_the_bundle_path_is_under_the_data_dir(manager) -> None:
    assert manager.bundle_path("t1") == manager.settings.data_dir / "bundles" / "t1.bundle"
