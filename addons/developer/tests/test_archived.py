"""An archived repository is retired: the manager refuses a task on it.

The fake git host of conftest answers the repository lookup. Nothing leaves
the process.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import FakeGitHost, make_settings
from joshua_developer.locks import LockManager
from joshua_developer.manager import Manager
from joshua_developer.runtime import StubRuntime
from joshua_developer.store import TaskStore

REPO = "github.com/example-home/app"
OLD = "github.com/example-home/old"
GL_OLD = "gitlab.example.net/group/old"

CONFIG = {
    "repos": ["github.com/example-home/*", "gitlab.example.net/*"],
    "platforms": {
        "github.com": {"kind": "github", "token_env": "GITHUB_TOKEN"},
        "gitlab.example.net": {"kind": "gitlab", "token_env": "GITLAB_TOKEN"},
    },
    "refuse_archived": True,
}


@pytest.fixture
def manager(tmp_path):
    store = TaskStore(make_settings(tmp_path, config=CONFIG).db_path)
    manager = Manager(make_settings(tmp_path, config=CONFIG), store, LockManager())
    manager.runtime = StubRuntime(manager, delay_s=None)
    yield manager
    store.close()


def _archived(host: FakeGitHost) -> None:
    host.override("GET", r"/repos/example-home/old$", httpx.Response(200, json={"archived": True}))
    host.override("GET", r"/repos/example-home/app$", httpx.Response(200, json={"archived": False}))
    host.override("GET", r"/projects/group%2Fold$", httpx.Response(200, json={"archived": True}))


async def test_a_task_on_an_archived_repository_is_refused(manager, git_host) -> None:
    _archived(git_host)
    github = await manager.develop("alex", OLD, "Add a route")
    gitlab = await manager.develop("alex", GL_OLD, "Add a route")
    for result in (github, gitlab):
        assert result["status"] == "rejected"
        assert result["reason"] == "repo_archived"
        assert "retired" in result["message"]
    assert manager.store.list_tasks(None) == []


async def test_a_rework_on_an_archived_repository_is_refused(manager, git_host) -> None:
    _archived(git_host)
    result = await manager.rework("alex", OLD, 3, "fix it")
    assert result["reason"] == "repo_archived"
    # The archive check comes before the pull request lookup.
    assert not any("/pulls/" in call for call in git_host.calls())


async def test_an_archived_reference_is_refused(manager, git_host) -> None:
    _archived(git_host)
    result = await manager.develop("alex", REPO, "Copy the pipeline", refs=[OLD])
    assert result["reason"] == "repo_archived"
    assert OLD in result["message"]


async def test_a_live_repository_and_a_silent_host_let_the_task_go(manager, git_host) -> None:
    _archived(git_host)
    live = await manager.develop("alex", REPO, "Add a route")
    assert live["status"] == "dispatched", live
    # The fake host answers 404 for this one: the check cannot say, so it lets
    # the task go on.
    silent = await manager.develop("alex", "github.com/example-home/other", "Add a route")
    assert silent["status"] == "dispatched", silent


async def test_the_check_can_be_turned_off(tmp_path, git_host) -> None:
    _archived(git_host)
    settings = make_settings(tmp_path, config={**CONFIG, "refuse_archived": False})
    store = TaskStore(settings.db_path)
    manager = Manager(settings, store, LockManager())
    manager.runtime = StubRuntime(manager, delay_s=None)
    try:
        result = await manager.develop("alex", OLD, "Add a route")
    finally:
        store.close()
    assert result["status"] == "dispatched", result
    assert git_host.calls("GET") == []
