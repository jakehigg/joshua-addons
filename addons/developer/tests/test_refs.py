"""Reference repositories (refs, #75): cloned read-only next to the checkout, never pushed."""

from __future__ import annotations

import pytest
from conftest import ALEX, CONFIG, GIT_TOKENS, make_settings, mcp_session, payload
from joshua_developer.locks import LockManager
from joshua_developer.manager import MAX_REFS, Manager
from joshua_developer.runtime import Report, StubRuntime
from joshua_developer.server import build_app
from joshua_developer.store import TaskStore
from joshua_developer.worker_api import build_brief

REPO = "github.com/example-home/app"
CHAT = "gitlab.example.net/joshua/joshua-chat"


@pytest.fixture
def manager(data_dir):
    settings = make_settings(
        data_dir, config={"repos": [*CONFIG["repos"], "gitlab.example.net/joshua/*"]}
    )
    store = TaskStore(settings.db_path)
    manager = Manager(settings, store, LockManager())
    manager.runtime = StubRuntime(manager, delay_s=None)
    yield manager
    store.close()


async def test_develop_keeps_the_normalized_refs(manager: Manager) -> None:
    result = await manager.develop(
        "alex",
        REPO,
        "Port the chat backend.",
        refs=[f"https://{CHAT}.git", "github.com/example-home/lib"],
    )
    assert result["status"] == "dispatched", result
    row = manager.store.get_task(result["task_id"])
    assert row is not None
    assert row["refs"] == [CHAT, "github.com/example-home/lib"]


async def test_a_task_without_refs_has_none(manager: Manager) -> None:
    result = await manager.develop("alex", REPO, "A plain task.")
    assert manager.store.get_task(result["task_id"])["refs"] is None  # type: ignore[index]


@pytest.mark.parametrize(
    ("refs", "reason", "words"),
    [
        (["github.com/someone-else/secret"], "repo_not_allowed", "refs:"),
        (["not a repo"], "invalid_arguments", "refs:"),
        ([REPO], "invalid_arguments", "is the task repository"),
        ([CHAT, CHAT], "invalid_arguments", "named twice"),
        (
            [f"github.com/example-home/r{n}" for n in range(MAX_REFS + 1)],
            "invalid_arguments",
            "at most",
        ),
    ],
)
async def test_a_bad_ref_is_rejected(
    manager: Manager, refs: list[str], reason: str, words: str
) -> None:
    result = await manager.develop("alex", REPO, "Port.", refs=refs)
    assert result["status"] == "rejected"
    assert result["reason"] == reason
    assert words in result["message"]
    assert manager.store.count_active_tasks() == 0


async def test_a_ref_on_a_host_with_no_token_is_rejected(manager: Manager, monkeypatch) -> None:
    monkeypatch.delenv("GITLAB_TOKEN")
    result = await manager.develop("alex", REPO, "Port.", refs=[CHAT])
    assert result["status"] == "rejected"
    assert result["reason"] == "token_missing"


async def test_the_brief_has_the_refs_and_one_credential_for_each_other_host(
    manager: Manager,
) -> None:
    result = await manager.develop(
        "alex", REPO, "Port.", refs=[CHAT, "github.com/example-home/lib"]
    )
    full = manager.store.get_task_full(result["task_id"])
    assert full is not None
    brief = build_brief(manager, full)
    assert brief["refs"] == [
        {"repo": CHAT, "repo_url": f"https://{CHAT}.git"},
        {
            "repo": "github.com/example-home/lib",
            "repo_url": "https://github.com/example-home/lib.git",
        },
    ]
    # github.com is the task host: its token is git_token. Only GitLab is extra.
    assert brief["ref_credentials"] == [
        {"host": "gitlab.example.net", "username": "oauth2", "token": GIT_TOKENS["GITLAB_TOKEN"]}
    ]


async def test_a_resumed_task_keeps_its_refs(manager: Manager) -> None:
    result = await manager.develop("alex", REPO, "Port.", branch="feature", refs=[CHAT])
    task_id = result["task_id"]
    report = Report(status="blocked", open_question="Which port?", pushed=True, head="feature")
    await manager.record_report(task_id, report)
    manager.runtime._active.discard(task_id)  # type: ignore[union-attr]
    resumed = await manager.answer("alex", {"task_id": task_id}, "8080")
    assert resumed["resumed"] is True
    full = manager.store.get_task_full(task_id)
    assert full is not None and full["refs"] == [CHAT]
    assert build_brief(manager, full)["refs"][0]["repo"] == CHAT


async def test_rework_on_a_git_host_takes_refs(data_dir) -> None:
    settings = make_settings(
        data_dir,
        config={
            "repos": ["git.example.org/*/*", "github.com/example-home/*"],
            "platforms": {
                "github.com": {"kind": "github", "token_env": "GITHUB_TOKEN"},
                "git.example.org": {"kind": "git", "token_env": "GITHUB_TOKEN"},
            },
        },
    )
    store = TaskStore(settings.db_path)
    manager = Manager(settings, store, LockManager())
    manager.runtime = StubRuntime(manager, delay_s=None)
    try:
        result = await manager.rework(
            "alex", "git.example.org/g/p", None, "More.", branch="feature", refs=[REPO]
        )
        assert result["status"] == "dispatched", result
        assert store.get_task(result["task_id"])["refs"] == [REPO]  # type: ignore[index]
        refused = await manager.rework(
            "alex", "git.example.org/g/p", None, "More.", branch="other", refs=["x"]
        )
        assert refused["status"] == "rejected"
    finally:
        store.close()


async def test_rework_of_a_pull_request_takes_refs(manager: Manager) -> None:
    result = await manager.rework("alex", REPO, 7, "Use the source.", refs=[CHAT])
    assert result["status"] == "dispatched", result
    assert manager.store.get_task(result["task_id"])["refs"] == [CHAT]  # type: ignore[index]


async def test_the_develop_tool_takes_refs_and_task_status_shows_them(settings) -> None:
    app = build_app(settings, stub_delay_s=None)
    async with mcp_session(app, ALEX) as session:
        started = payload(
            await session.call_tool(
                "develop",
                {
                    "repo": REPO,
                    "brief": "Port the lib.",
                    "refs": ["github.com/example-home/lib"],
                },
            )
        )
        status = payload(await session.call_tool("task_status", {"task_id": started["task_id"]}))
    assert status["refs"] == ["github.com/example-home/lib"]
