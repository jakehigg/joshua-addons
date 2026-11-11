"""The work directories: one for each task, deleted only after a success, kept and reused
otherwise, and swept after work.keep_failed days."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from pathlib import Path

import pytest
from conftest import CONFIG, make_settings
from joshua_developer.config import ConfigError, settings_from_env
from joshua_developer.locks import LockManager
from joshua_developer.manager import Manager
from joshua_developer.runtime import Report, StubRuntime
from joshua_developer.runtime_docker import DockerRuntime, worker_environment
from joshua_developer.runtime_k8s import job_manifest
from joshua_developer.store import TaskStore
from joshua_developer.workspaces import Workspaces

REPO = "github.com/example-home/app"
DAY = 86400


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


def git(*args: str, cwd: Path) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.test", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def commit_in(workspace: Path) -> str:
    """Make a checkout with one commit in ``workspace``, as a worker does. Returns the commit."""
    checkout = workspace / "repo"
    checkout.mkdir()
    git("init", "-q", "-b", "feature", cwd=checkout)
    (checkout / "a.txt").write_text("a\n")
    git("add", "a.txt", cwd=checkout)
    git("commit", "-q", "-m", "add a", cwd=checkout)
    return git("rev-parse", "HEAD", cwd=checkout)


async def develop(manager: Manager, person: str = "alex", branch: str = "feature") -> str:
    result = await manager.develop(person, REPO, "Add a health check.", branch=branch)
    assert result["status"] == "dispatched", result
    return result["task_id"]


async def finish(manager: Manager, task_id: str, status: str) -> None:
    await manager.record_report(task_id, Report(status=status))  # type: ignore[arg-type]
    manager.runtime._active.discard(task_id)  # type: ignore[union-attr]


async def test_each_task_gets_its_own_directory(manager: Manager, work_dir: Path) -> None:
    first = await develop(manager, branch="one")
    second = await develop(manager, branch="two")
    assert (work_dir / first).is_dir() and (work_dir / second).is_dir()
    assert oct((work_dir / first).stat().st_mode & 0o777) == oct(0o700)


async def test_a_successful_task_loses_its_directory(manager: Manager, work_dir: Path) -> None:
    task_id = await develop(manager)
    commit_in(work_dir / task_id)
    await finish(manager, task_id, "success")
    assert not (work_dir / task_id).exists()


@pytest.mark.parametrize("status", ["failed", "blocked", "timed_out"])
async def test_a_task_that_did_not_succeed_keeps_its_commits(
    manager: Manager, work_dir: Path, status: str
) -> None:
    task_id = await develop(manager)
    commit = commit_in(work_dir / task_id)
    await finish(manager, task_id, status)
    assert git("rev-parse", "feature", cwd=work_dir / task_id / "repo") == commit


async def test_a_manager_restart_keeps_the_directory(
    manager: Manager, work_dir: Path, data_dir: Path
) -> None:
    task_id = await develop(manager)
    commit = commit_in(work_dir / task_id)
    restarted = Manager(manager.settings, manager.store, LockManager())
    restarted.runtime = StubRuntime(restarted, delay_s=None)
    assert restarted.recover() == [task_id]
    assert await restarted.reattach_workers() == [task_id]
    assert git("rev-parse", "HEAD", cwd=work_dir / task_id / "repo") == commit


async def test_a_redispatch_on_the_same_branch_takes_the_kept_directory(
    manager: Manager, work_dir: Path
) -> None:
    first = await develop(manager)
    commit = commit_in(work_dir / first)
    await finish(manager, first, "failed")

    second = await develop(manager)
    assert second != first
    assert not (work_dir / first).exists()
    assert git("rev-parse", "HEAD", cwd=work_dir / second / "repo") == commit


async def test_a_resumed_task_keeps_its_own_directory(manager: Manager, work_dir: Path) -> None:
    task_id = await develop(manager)
    commit = commit_in(work_dir / task_id)
    report = Report(status="blocked", open_question="Which port?", pushed=True, head="feature")
    await manager.record_report(task_id, report)
    manager.runtime._active.discard(task_id)  # type: ignore[union-attr]
    result = await manager.answer("alex", {"task_id": task_id}, "8080")
    assert result["resumed"] is True
    assert git("rev-parse", "HEAD", cwd=work_dir / task_id / "repo") == commit


async def test_an_answer_resumes_a_task_a_restart_failed_from_its_kept_checkout(
    manager: Manager, work_dir: Path
) -> None:
    task_id = await develop(manager)
    commit = commit_in(work_dir / task_id)
    restarted = Manager(manager.settings, manager.store, LockManager())
    restarted.runtime = StubRuntime(restarted, delay_s=None)
    restarted.recover()
    await restarted.reattach_workers()
    row = restarted.store.get_task(task_id)
    assert row is not None and row["status"] == "failed"
    result = await restarted.answer("alex", {"task_id": task_id}, "Go on.")
    assert result["resumed"] is True and result["status"] == "dispatched"
    assert git("rev-parse", "HEAD", cwd=work_dir / task_id / "repo") == commit


async def test_a_failed_task_with_an_empty_directory_is_not_resumable(
    manager: Manager, work_dir: Path
) -> None:
    task_id = await develop(manager)
    await finish(manager, task_id, "failed")
    result = await manager.answer("alex", {"task_id": task_id}, "Go on.")
    assert result["reason"] == "not_waiting"
    manager.store.update_task(task_id, status="blocked")
    result = await manager.answer("alex", {"task_id": task_id}, "Go on.")
    assert result["reason"] == "not_resumable"


async def test_another_branch_or_another_person_gets_a_fresh_directory(
    manager: Manager, work_dir: Path
) -> None:
    first = await develop(manager)
    commit_in(work_dir / first)
    await finish(manager, first, "failed")

    other_branch = await develop(manager, branch="other")
    other_person = await develop(manager, person="mia")
    assert (work_dir / first / "repo").is_dir()
    assert list((work_dir / other_branch).iterdir()) == []
    assert list((work_dir / other_person).iterdir()) == []


async def test_a_successful_task_is_never_reused(manager: Manager, work_dir: Path) -> None:
    first = await develop(manager)
    await finish(manager, first, "success")
    second = await develop(manager)
    assert list((work_dir / second).iterdir()) == []


async def test_a_directory_that_cannot_be_made_fails_the_task(
    data_dir: Path, tmp_path: Path
) -> None:
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("")
    settings = make_settings(data_dir, work_dir=blocked)
    store = TaskStore(settings.db_path)
    manager = Manager(settings, store, LockManager())
    manager.runtime = StubRuntime(manager, delay_s=None)
    try:
        result = await manager.develop("alex", REPO, "Add a health check.", branch="feature")
        assert result["status"] == "failed"
        row = store.get_task(result["task_id"])
        assert row is not None and row["status"] == "failed"
        assert "the work directory was not made" in row["error"]
        assert manager.locks.get(REPO, "branch:feature") is None
    finally:
        store.close()


async def test_no_work_dir_makes_no_directory(data_dir: Path, tmp_path: Path) -> None:
    settings = make_settings(data_dir)
    store = TaskStore(settings.db_path)
    manager = Manager(settings, store, LockManager())
    manager.runtime = StubRuntime(manager, delay_s=None)
    try:
        task_id = await develop(manager)
        await finish(manager, task_id, "success")
        assert manager.workspaces.enabled is False
        assert manager.workspaces.drop(task_id) is True
        assert manager.workspaces.sweep() == 0
    finally:
        store.close()


# --- the sweeper -------------------------------------------------------------


def _age(path: Path, days: float) -> None:
    stamp = time.time() - days * DAY
    os.utime(path, (stamp, stamp))


async def test_the_sweeper_deletes_only_the_directories_past_the_age(
    manager: Manager, work_dir: Path, caplog
) -> None:
    old = await develop(manager, branch="old")
    await finish(manager, old, "failed")
    recent = await develop(manager, branch="recent")
    await finish(manager, recent, "blocked")
    active = await develop(manager, branch="active")
    eight_days_ago = "2026-01-01T00:00:00+00:00"
    manager.store.update_task(old, completed_at=eight_days_ago)
    _age(work_dir / active, 30)
    orphan_old = work_dir / "00000000-0000-4000-8000-000000000001"
    orphan_old.mkdir()
    _age(orphan_old, 8)
    orphan_new = work_dir / "00000000-0000-4000-8000-000000000002"
    orphan_new.mkdir()

    caplog.set_level(logging.INFO)
    sweeper = Workspaces(work_dir, manager.store, keep_days=7, now=time.time)
    assert sweeper.sweep() == 2
    assert not (work_dir / old).exists() and not orphan_old.exists()
    assert (work_dir / recent).is_dir() and (work_dir / active).is_dir() and orphan_new.is_dir()
    # The sweeper logs each deletion, with the task and the reason.
    assert caplog.text.count("workspace deleted") == 2
    assert old in caplog.text and "expired" in caplog.text and "no_task" in caplog.text


async def test_the_sweeper_takes_the_age_from_work_keep_failed(
    data_dir: Path, work_dir: Path
) -> None:
    settings = make_settings(data_dir, work_dir=work_dir, config={"work": {"keep_failed": 30}})
    store = TaskStore(settings.db_path)
    manager = Manager(settings, store, LockManager())
    try:
        orphan = work_dir / "00000000-0000-4000-8000-000000000003"
        orphan.mkdir()
        _age(orphan, 8)
        assert manager.sweep_workspaces() == 0
        _age(orphan, 31)
        assert manager.sweep_workspaces() == 1
    finally:
        store.close()


async def test_the_sweeper_deletes_a_success_directory_that_drop_left(
    manager: Manager, work_dir: Path, monkeypatch
) -> None:
    task_id = await develop(manager)
    commit_in(work_dir / task_id)

    def refuse(path: Path) -> None:
        raise OSError("busy")

    monkeypatch.setattr("joshua_developer.workspaces.shutil.rmtree", refuse)
    await finish(manager, task_id, "success")
    assert (work_dir / task_id).is_dir()
    assert manager.sweep_workspaces() == 0
    monkeypatch.undo()
    assert manager.sweep_workspaces() == 1
    assert not (work_dir / task_id).exists()


async def test_the_sweeper_never_touches_a_name_that_is_not_a_task_id(
    manager: Manager, work_dir: Path
) -> None:
    for name in ("lost+found", "notes", "12345678-AAAA-4000-8000-000000000000"):
        (work_dir / name).mkdir()
        _age(work_dir / name, 30)
    assert manager.sweep_workspaces() == 0
    assert (work_dir / "lost+found").is_dir() and (work_dir / "notes").is_dir()


async def test_the_sweeper_skips_files_and_symlinks(manager: Manager, work_dir: Path) -> None:
    target = work_dir.parent / "outside"
    target.mkdir()
    _age(target, 30)
    (work_dir / "link").symlink_to(target)
    (work_dir / "file").write_text("")
    assert manager.sweep_workspaces() == 0
    assert target.is_dir()


async def test_a_failed_sweep_is_logged_and_not_fatal(manager: Manager, monkeypatch) -> None:
    def broken() -> int:
        raise PermissionError("denied")

    monkeypatch.setattr(manager.workspaces, "sweep", broken)
    assert manager.sweep_workspaces() == 0


async def test_the_sweeper_runs_on_an_interval(manager: Manager, monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(manager, "sweep_workspaces", lambda: calls.append(1) or 0)
    loop = asyncio.create_task(manager.sweep_forever(interval_s=0.01))
    for _ in range(200):
        if len(calls) >= 2:
            break
        await asyncio.sleep(0.01)
    loop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop
    assert len(calls) >= 2


# --- the runtimes ------------------------------------------------------------

TASK = {"task_id": "12345678-aaaa-bbbb-cccc-dddddddddddd", "persona": "sonnet", "worker_token": "t"}


def test_the_job_mounts_the_task_directory_of_the_claim(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        worker_image="example/worker:1",
        worker_runtime="kubernetes",
        work_dir=Path("/work"),
        worker_work_claim="developer-work",
    )
    pod = job_manifest(settings, TASK, "joshua", 60)["spec"]["template"]["spec"]
    [work, tmp] = pod["volumes"]
    assert work == {"name": "work", "persistentVolumeClaim": {"claimName": "developer-work"}}
    assert tmp["emptyDir"]
    mounts = pod["containers"][0]["volumeMounts"]
    assert mounts[0] == {"name": "work", "mountPath": "/work", "subPath": TASK["task_id"]}
    assert "subPath" not in mounts[1]
    assert pod["securityContext"]["fsGroupChangePolicy"] == "OnRootMismatch"
    env = {e["name"]: e["value"] for e in pod["containers"][0]["env"]}
    assert env["HOME"] == "/tmp/home" and env["CLAUDE_CONFIG_DIR"] == "/tmp/home/.claude"


def test_without_a_claim_the_job_keeps_the_emptydir(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, worker_image="example/worker:1", worker_runtime="kubernetes")
    pod = job_manifest(settings, TASK, "joshua", 60)["spec"]["template"]["spec"]
    assert pod["volumes"][0] == {"name": "work", "emptyDir": {"sizeLimit": "4Gi"}}
    assert "subPath" not in pod["containers"][0]["volumeMounts"][0]
    assert "HOME" not in worker_environment(settings, TASK)


@pytest.mark.parametrize("storage_opt", [False, True])
def test_the_container_mounts_the_task_directory_of_the_volume(
    tmp_path: Path, storage_opt: bool
) -> None:
    settings = make_settings(
        tmp_path,
        worker_image="example/worker:1",
        worker_runtime="docker",
        work_dir=Path("/work"),
        worker_work_volume="developer-work",
        config={"worker": {"disk_docker_storage_opt": storage_opt}},
    )
    runtime = DockerRuntime(_Reporter(), settings, client=object())  # type: ignore[arg-type]
    kwargs = runtime._create_kwargs(TASK)
    work = kwargs["mounts"][0]
    assert (work["Target"], work["Source"], work["Type"]) == ("/work", "developer-work", "volume")
    assert work["VolumeOptions"]["Subpath"] == TASK["task_id"]
    targets = [m["Target"] for m in kwargs["mounts"]]
    assert targets == (["/work"] if storage_opt else ["/work", "/tmp"])
    assert kwargs["environment"]["HOME"] == "/tmp/home"


class _Reporter:
    def is_active(self, task_id: str, worker_token: str | None = None) -> bool:
        return True


# --- the settings ------------------------------------------------------------


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    import yaml

    path = tmp_path / "developer.yaml"
    path.write_text(yaml.safe_dump(CONFIG))
    return {"DEVELOPER_CONFIG": str(path), "WORKER_IMAGE": "example/worker:1", **extra}


def test_the_settings_read_the_work_storage(tmp_path: Path) -> None:
    settings = settings_from_env(
        _env(
            tmp_path,
            WORKER_RUNTIME="kubernetes",
            DEVELOPER_WORK_DIR="/work",
            WORKER_WORK_CLAIM="developer-work",
        )
    )
    assert settings.work_dir == Path("/work")
    assert settings.worker_work_claim == "developer-work"
    assert settings.config.work.keep_failed == 7
    docker = settings_from_env(
        _env(
            tmp_path,
            WORKER_RUNTIME="docker",
            DEVELOPER_WORK_DIR="/work",
            WORKER_WORK_VOLUME="developer-work",
        )
    )
    assert docker.worker_work_volume == "developer-work"
    assert settings_from_env(_env(tmp_path)).work_dir is None


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"DEVELOPER_WORK_DIR": "work"}, "absolute path"),
        (
            {"WORKER_RUNTIME": "kubernetes", "DEVELOPER_WORK_DIR": "/work"},
            "WORKER_WORK_CLAIM is required",
        ),
        (
            {"WORKER_RUNTIME": "docker", "DEVELOPER_WORK_DIR": "/work"},
            "WORKER_WORK_VOLUME is required",
        ),
        ({"WORKER_RUNTIME": "kubernetes", "WORKER_WORK_CLAIM": "c"}, "DEVELOPER_WORK_DIR is"),
        ({"WORKER_RUNTIME": "docker", "WORKER_WORK_CLAIM": "c"}, "only when WORKER_RUNTIME"),
        ({"WORKER_RUNTIME": "kubernetes", "WORKER_WORK_VOLUME": "v"}, "only when WORKER_RUNTIME"),
        ({"WORKER_RUNTIME": "kubernetes", "WORKER_WORK_CLAIM": "Bad_Name"}, "PersistentVolume"),
        ({"WORKER_RUNTIME": "docker", "WORKER_WORK_VOLUME": "/bad"}, "Docker volume"),
    ],
)
def test_bad_work_storage_settings_stop_the_start(
    tmp_path: Path, extra: dict[str, str], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        settings_from_env(_env(tmp_path, **extra))


def test_keep_failed_must_be_a_positive_number_of_days(tmp_path: Path) -> None:
    from joshua_developer.config import parse_config

    with pytest.raises(ConfigError, match="work.keep_failed"):
        parse_config({**CONFIG, "work": {"keep_failed": 0}})
    with pytest.raises(ConfigError, match="work.keep_failed"):
        parse_config({**CONFIG, "work": {"keep_failed": "7d"}})
