"""The manager and the git host: the allowlist and the token at dispatch, the
rework branch, and the gate after a push (scan, then pull request).

The fake git host of conftest answers every adapter call. Nothing leaves the
process.
"""

from __future__ import annotations

import json

import httpx
import pytest
from conftest import (
    ALEX,
    GIT_TOKENS,
    FakeGitHost,
    github_pr,
    gitlab_mr,
    make_settings,
    mcp_session,
    payload,
)
from joshua_developer import notify
from joshua_developer.locks import LockManager
from joshua_developer.manager import Manager, finding_error, retry_5xx
from joshua_developer.platforms import PlatformError
from joshua_developer.runtime import Report, StubRuntime
from joshua_developer.scan import Finding
from joshua_developer.server import build_app
from joshua_developer.store import TaskStore
from joshua_developer.worker_api import build_worker_app
from pydantic import ValidationError

REPO = "github.com/example-home/app"
GL_REPO = "gitlab.example.net/group/sub/project"
GIT_REPO = "git.example.org/team/app"
SECRET = "gh" + "p_" + "Z" * 36
CLONE_HEAD = "c0ffee" + "0" * 34

CONFIG = {
    "repos": ["github.com/example-home/*", "gitlab.example.net/*", "git.example.org/*/*"],
    "platforms": {
        "github.com": {"kind": "github", "token_env": "GITHUB_TOKEN"},
        "gitlab.example.net": {"kind": "gitlab", "token_env": "GITLAB_TOKEN"},
        "git.example.org": {"kind": "git", "token_env": "GITLAB_TOKEN"},
    },
}


@pytest.fixture
def manager(tmp_path):
    settings = make_settings(tmp_path, config=CONFIG)
    store = TaskStore(settings.db_path)
    manager = Manager(settings, store, LockManager())
    manager.runtime = StubRuntime(manager, delay_s=None)
    yield manager
    store.close()


@pytest.fixture
def reports(monkeypatch) -> list[dict]:
    """The task rows that went to channels as reports."""
    sent: list[dict] = []

    async def fake_send(settings, task, default=None) -> bool:
        sent.append(task)
        return True

    monkeypatch.setattr(notify, "send_report", fake_send)
    return sent


def clean_file(path: str = "app.py") -> dict:
    return {"filename": path, "patch": "@@ -1 +1,2 @@\n x\n+y = 1", "additions": 1}


def pushed(**extra) -> Report:
    fields = {
        "status": "success",
        "summary": "Added the route.",
        "files_changed": ["app.py"],
        "tests_run": ["pytest"],
        "pushed": True,
    }
    fields.update(extra)
    return Report.model_validate(fields)


async def develop(manager: Manager, repo: str = REPO, **extra) -> dict:
    brief = extra.pop("brief", "Add a /healthz route\n\nDetails here.")
    result = await manager.develop("alex", repo, brief, **extra)
    assert result["status"] == "dispatched", result
    task = manager.store.get_task_full(result["task_id"])
    assert task is not None
    return task


# --- dispatch --------------------------------------------------------------


async def test_a_repo_outside_the_list_is_refused_before_any_network(
    manager, git_host: FakeGitHost
) -> None:
    result = await manager.rework("alex", "github.com/evil/app", 3, "fix")
    other = await manager.develop("alex", "github.com/evil/app", "brief")
    assert result["reason"] == other["reason"] == "repo_not_allowed"
    assert git_host.requests == []
    assert manager.store.list_tasks(None) == []


async def test_a_host_with_no_platform_is_refused(tmp_path, git_host: FakeGitHost) -> None:
    settings = make_settings(tmp_path, config={"repos": ["*"]})
    store = TaskStore(settings.db_path)
    manager = Manager(settings, store, LockManager())
    manager.runtime = StubRuntime(manager, delay_s=None)
    try:
        result = await manager.develop("mia", "codeberg.org/a/b", "brief")
    finally:
        store.close()
    assert result["status"] == "rejected"
    assert result["reason"] == "repo_not_configured"
    assert "codeberg.org" in result["message"]
    assert "github.com" not in result["message"]
    assert git_host.requests == []


async def test_a_missing_token_is_refused_by_variable(manager, monkeypatch, git_host) -> None:
    monkeypatch.delenv("GITHUB_TOKEN_ALEX")
    monkeypatch.setenv("GITLAB_TOKEN", "")
    result = await manager.develop("alex", REPO, "brief")
    rework = await manager.rework("alex", GL_REPO, 3, "fix")
    for rejected in (result, rework):
        assert rejected["status"] == "rejected"
        assert rejected["reason"] == "token_missing"
        for value in GIT_TOKENS.values():
            assert value not in json.dumps(rejected)
    assert "GITHUB_TOKEN_ALEX" in result["message"]
    assert "GITLAB_TOKEN" in rework["message"]
    assert git_host.requests == []
    assert manager.store.list_tasks(None) == []


async def test_develop_names_its_branch_from_the_brief(manager) -> None:
    task = await develop(manager, brief="Fix: the «login» page crashes!\nmore")
    assert task["branch_name"] == f"joshua/fix-the-login-page-crashes-{task['task_id'][:6]}"
    assert task["base_branch"] == "main"
    named = await develop(manager, branch="feature/x", base_branch="develop")
    assert (named["branch_name"], named["base_branch"]) == ("feature/x", "develop")


async def test_rework_resolves_the_branch_at_dispatch(manager, git_host: FakeGitHost) -> None:
    git_host.override(
        "GET",
        r"/pulls/5$",
        httpx.Response(200, json=github_pr(5, "feature/login", "develop")),
    )
    result = await manager.rework("alex", REPO, "https://github.com/example-home/app/pull/5", "x")
    task = manager.store.get_task_full(result["task_id"])
    assert task is not None
    assert (task["branch_name"], task["base_branch"]) == ("feature/login", "develop")
    assert task["pr_number"] == 5
    assert task["pr_url"] == "https://github.com/example-home/app/pull/5"
    assert git_host.calls() == ["GET /repos/example-home/app/pulls/5"]
    [request] = git_host.requests
    assert request.headers["authorization"] == f"Bearer {GIT_TOKENS['GITHUB_TOKEN_ALEX']}"


async def test_rework_of_a_merge_request_on_gitlab(manager, git_host: FakeGitHost) -> None:
    git_host.override(
        "GET", r"/merge_requests/8$", httpx.Response(200, json=gitlab_mr(8, "fix/x", "develop"))
    )
    result = await manager.rework("alex", GL_REPO, 8, "x")
    task = manager.store.get_task_full(result["task_id"])
    assert task is not None
    assert (task["branch_name"], task["base_branch"]) == ("fix/x", "develop")
    assert task["pr_url"] == "https://gitlab.example.net/g/p/-/merge_requests/8"
    assert git_host.calls() == ["GET /api/v4/projects/group%2Fsub%2Fproject/merge_requests/8"]


async def test_rework_of_an_unknown_pr_is_refused(manager, git_host: FakeGitHost) -> None:
    git_host.override("GET", r"/pulls/99$", httpx.Response(404, json={"message": "Not Found"}))
    result = await manager.rework("alex", REPO, 99, "x")
    assert result["status"] == "rejected"
    assert result["reason"] == "pr_not_found"
    assert manager.store.list_tasks(None) == []


async def test_rework_of_a_closed_pr_is_refused(manager, git_host: FakeGitHost) -> None:
    git_host.override(
        "GET", r"/pulls/4$", httpx.Response(200, json=github_pr(4, "b", state="closed"))
    )
    result = await manager.rework("alex", REPO, 4, "x")
    assert result["reason"] == "pr_not_open"


async def test_rework_of_a_pr_with_a_bad_branch_is_refused(manager, git_host) -> None:
    git_host.override("GET", r"/pulls/4$", httpx.Response(200, json=github_pr(4, "a..b")))
    result = await manager.rework("alex", REPO, 4, "x")
    assert result["reason"] == "platform_error"


async def test_rework_when_the_host_fails_twice(manager, git_host: FakeGitHost) -> None:
    git_host.override("GET", r"/pulls/4$", httpx.Response(503, json={"message": "down"}))
    result = await manager.rework("alex", REPO, 4, "x")
    assert result["reason"] == "platform_error"
    assert "HTTP 503: down" in result["message"]
    assert len(git_host.requests) == 2


async def test_rework_needs_pr_on_a_host_with_an_api(manager) -> None:
    result = await manager.rework("alex", REPO, None, "x")
    assert result["reason"] == "invalid_arguments"
    assert result["message"] == "pr is required"


async def test_rework_on_a_git_host_takes_a_branch(manager, git_host: FakeGitHost) -> None:
    no_branch = await manager.rework("alex", GIT_REPO, 3, "x")
    assert no_branch["reason"] == "invalid_arguments"
    assert "needs branch" in no_branch["message"]
    result = await manager.rework("alex", GIT_REPO, None, "x", branch="feature/y")
    task = manager.store.get_task_full(result["task_id"])
    assert task is not None
    assert task["scope"] == "branch:feature/y"
    assert task["branch_name"] == "feature/y"
    assert task["pr_number"] is None
    assert git_host.requests == []


@pytest.mark.parametrize(
    ("branch", "base"),
    [("main", None), ("master", None), ("develop", "develop"), ("main", "develop")],
)
async def test_develop_never_targets_a_base_branch(manager, git_host, branch, base) -> None:
    result = await manager.develop("alex", REPO, "brief", branch=branch, base_branch=base)
    assert (result["status"], result["reason"]) == ("rejected", "branch_is_base")
    assert manager.store.list_tasks(None) == []
    assert git_host.requests == []


@pytest.mark.parametrize(("source", "base"), [("develop", "develop"), ("main", "develop")])
async def test_rework_of_a_pr_from_a_base_branch_is_refused(manager, git_host, source, base):
    git_host.override("GET", r"/pulls/4$", httpx.Response(200, json=github_pr(4, source, base)))
    result = await manager.rework("alex", REPO, 4, "x")
    assert (result["status"], result["reason"]) == ("rejected", "branch_is_base")
    assert manager.store.list_tasks(None) == []


@pytest.mark.parametrize("branch", ["main", "master"])
async def test_rework_on_a_git_host_never_targets_the_default_branch(
    manager, git_host, branch
) -> None:
    result = await manager.rework("alex", GIT_REPO, None, "x", branch=branch)
    assert (result["status"], result["reason"]) == ("rejected", "branch_is_base")
    assert manager.store.list_tasks(None) == []


async def test_rework_over_mcp_with_a_branch(tmp_path) -> None:
    app = build_app(make_settings(tmp_path, config=CONFIG), stub_delay_s=None)
    async with mcp_session(app, ALEX) as session:
        result = payload(
            await session.call_tool(
                "rework", {"repo": GIT_REPO, "feedback": "x", "branch": "feature/y"}
            )
        )
        status = payload(await session.call_tool("task_status", {"task_id": result["task_id"]}))
    assert result["status"] == "dispatched"
    assert status["branch_name"] == "feature/y"
    assert status["scan"] is None


# --- the brief -------------------------------------------------------------


async def test_the_brief_carries_the_git_credential(manager) -> None:
    task = await develop(manager, GL_REPO)
    transport = httpx.ASGITransport(app=build_worker_app(manager))
    headers = {"Authorization": f"Bearer {task['worker_token']}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://m", headers=headers) as c:
        brief = (await c.get("/worker/brief")).json()
    assert brief["git_token"] == GIT_TOKENS["GITLAB_TOKEN"]
    assert brief["git_username"] == "oauth2"
    assert brief["platform_kind"] == "gitlab"
    assert brief["branch"] == task["branch_name"]


async def test_the_brief_is_refused_when_the_token_is_gone(manager, monkeypatch, caplog) -> None:
    task = await develop(manager)
    monkeypatch.delenv("GITHUB_TOKEN_ALEX")
    transport = httpx.ASGITransport(app=build_worker_app(manager))
    headers = {"Authorization": f"Bearer {task['worker_token']}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://m", headers=headers) as c:
        response = await c.get("/worker/brief")
    assert response.status_code == 503
    assert response.json() == {"error": "token_missing"}


async def test_the_brief_log_line_holds_no_contents(manager, caplog) -> None:
    import logging

    task = await develop(manager, brief="SECRET-BRIEF-TEXT")
    transport = httpx.ASGITransport(app=build_worker_app(manager))
    headers = {"Authorization": f"Bearer {task['worker_token']}"}
    with caplog.at_level(logging.DEBUG):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://m", headers=headers
        ) as c:
            assert (await c.get("/worker/brief")).status_code == 200
    text = " ".join(str(record.msg) for record in caplog.records)
    assert "brief served" in text
    assert "SECRET-BRIEF-TEXT" not in text
    assert GIT_TOKENS["GITHUB_TOKEN_ALEX"] not in text


# --- after the push --------------------------------------------------------


async def test_a_clean_push_opens_a_pull_request(manager, git_host, reports) -> None:
    git_host.compare_files = [clean_file()]
    task = await develop(manager)
    head = task["branch_name"]
    row = await manager.record_report(task["task_id"], pushed(head=head))
    assert row is not None
    assert row["status"] == "success"
    assert row["scan"] == "clean"
    assert row["pr_url"] == "https://github.com/example-home/app/pull/42"
    assert row["pr_number"] == 42
    assert git_host.calls() == [
        f"GET /repos/example-home/app/compare/main...{head}",
        f"GET /repos/example-home/app/pulls?head=example-home%3A{head.replace('/', '%2F')}"
        "&state=open",
        "POST /repos/example-home/app/pulls",
    ]
    body = json.loads(git_host.requests[-1].content)
    assert body["title"] == "Add a /healthz route"
    assert body["head"] == head and body["base"] == "main"
    assert "Added the route." in body["body"]
    assert "- app.py" in body["body"] and "- pytest" in body["body"]
    assert "Written by the Joshua developer for Alex Example" in body["body"]
    assert task["task_id"] in body["body"]
    assert [r["pr_url"] for r in reports] == [row["pr_url"]]
    assert manager.locks.get(REPO, task["scope"]) is None


async def test_the_pr_title_is_cut_to_72_characters(manager, git_host) -> None:
    task = await develop(manager, brief="Word " * 40)
    await manager.record_report(task["task_id"], pushed())
    title = json.loads(git_host.requests[-1].content)["title"]
    assert len(title) <= 72 and title.startswith("Word Word")


async def test_a_scan_hit_deletes_the_branch_and_fails(manager, git_host, reports) -> None:
    git_host.compare_files = [
        clean_file(),
        {"filename": "config.py", "patch": f"@@ -3 +3,2 @@\n a\n+KEY = '{SECRET}'"},
    ]
    task = await develop(manager, branch="feature/leak")
    row = await manager.record_report(
        task["task_id"], pushed(head="feature/leak", created_branch=True)
    )
    assert row is not None
    assert row["status"] == "failed"
    assert row["scan"] == "hit"
    assert row["error"] == (
        "the diff holds what looks like a credential (github_token in config.py:4)"
    )
    assert row["findings"] == [
        {"path": "config.py", "line_no": 4, "pattern_name": "github_token"},
    ]
    assert row["pr_url"] is None
    assert git_host.calls() == [
        "GET /repos/example-home/app/compare/main...feature/leak",
        "DELETE /repos/example-home/app/git/refs/heads/feature/leak",
    ]
    assert SECRET not in json.dumps(row)
    assert len(reports) == 1 and reports[0]["status"] == "failed"


async def test_a_scan_hit_whose_branch_will_not_delete(manager, git_host, reports) -> None:
    git_host.compare_files = [{"filename": "k", "patch": f"@@ -0,0 +1 @@\n+{SECRET}"}]
    git_host.override("DELETE", r"/refs/heads/", httpx.Response(403, json={"message": "no"}))
    task = await develop(manager, branch="leak")
    row = await manager.record_report(task["task_id"], pushed(created_branch=True))
    assert row is not None and row["status"] == "failed"
    assert "not deleted (HTTP 403: no); delete it by hand" in row["error"]


async def test_the_scan_reads_only_the_commits_of_the_worker(manager, git_host) -> None:
    task = await develop(manager, branch="feature/x")
    row = await manager.record_report(
        task["task_id"], pushed(head="feature/x", clone_head=CLONE_HEAD)
    )
    assert row is not None and row["status"] == "success"
    assert git_host.calls()[0] == (f"GET /repos/example-home/app/compare/{CLONE_HEAD}...feature/x")


@pytest.mark.parametrize("clone_head", [None, "main", "HEAD~3", "ABC" * 14, "a" * 39])
async def test_a_clone_head_that_is_not_a_commit_scans_from_the_base(
    manager, git_host, clone_head
) -> None:
    task = await develop(manager, branch="feature/x")
    await manager.record_report(task["task_id"], pushed(head="feature/x", clone_head=clone_head))
    assert git_host.calls()[0] == "GET /repos/example-home/app/compare/main...feature/x"


async def test_a_scan_hit_on_a_branch_that_was_there_keeps_it(manager, git_host) -> None:
    git_host.compare_files = [{"filename": "k", "patch": f"@@ -0,0 +1 @@\n+{SECRET}"}]
    task = await develop(manager, branch="feature/old")
    row = await manager.record_report(
        task["task_id"], pushed(head="feature/old", clone_head=CLONE_HEAD, created_branch=False)
    )
    assert row is not None and (row["status"], row["scan"]) == ("failed", "hit")
    assert "github_token in k:1" in row["error"]
    assert "kept, because it existed before the task" in row["error"]
    assert not [call for call in git_host.calls() if call.startswith("DELETE")]


async def test_a_scan_hit_on_a_rework_never_deletes(manager, git_host) -> None:
    git_host.compare_files = [{"filename": "k", "patch": f"@@ -0,0 +1 @@\n+{SECRET}"}]
    result = await manager.rework("alex", REPO, 42, "Fix it.")
    assert result["status"] == "dispatched", result
    # A worker that says it made the branch does not change the rule.
    row = await manager.record_report(result["task_id"], pushed(head="pr-42", created_branch=True))
    assert row is not None and (row["status"], row["scan"]) == ("failed", "hit")
    assert "kept, because it existed before the task" in row["error"]
    assert not [call for call in git_host.calls() if call.startswith("DELETE")]


async def test_a_resumed_develop_deletes_the_branch_an_earlier_worker_made(
    manager, git_host
) -> None:
    task = await develop(manager, branch="feature/new")
    task_id = task["task_id"]
    stop = pushed(status="blocked", head="feature/new", created_branch=True)
    await manager.record_report(task_id, stop)
    resumed = await manager.answer("alex", task, "Go on.")
    assert resumed["status"] == "dispatched", resumed
    git_host.compare_files = [{"filename": "k", "patch": f"@@ -0,0 +1 @@\n+{SECRET}"}]
    row = await manager.record_report(task_id, pushed(head="feature/new", created_branch=False))
    assert row is not None and row["status"] == "failed"
    assert "DELETE /repos/example-home/app/git/refs/heads/feature/new" in git_host.calls()


@pytest.mark.parametrize("field", ["pr_url", "pr_number", "branch"])
def test_the_report_has_no_field_for_the_branch_or_the_pr(field: str) -> None:
    with pytest.raises(ValidationError):
        Report.model_validate({"status": "success", field: "x"})


async def test_an_existing_pr_is_reused(manager, git_host) -> None:
    task = await develop(manager, branch="feature/x")
    git_host.override(
        "GET", r"/pulls$", httpx.Response(200, json=[github_pr(9, "feature/x", "main")])
    )
    row = await manager.record_report(task["task_id"], pushed())
    assert row is not None
    assert (row["status"], row["pr_number"]) == ("success", 9)
    assert row["pr_url"] == "https://github.com/example-home/app/pull/9"
    assert git_host.calls("POST") == []


async def test_a_gitlab_push_opens_a_merge_request(manager, git_host) -> None:
    git_host.compare_files = [{"new_path": "a.py", "diff": "@@ -1 +1 @@\n-a\n+b"}]
    task = await develop(manager, GL_REPO, branch="feature/x", base_branch="develop")
    row = await manager.record_report(task["task_id"], pushed())
    assert row is not None
    assert row["status"] == "success"
    assert row["pr_url"] == "https://gitlab.example.net/g/p/-/merge_requests/42"
    project = "/api/v4/projects/group%2Fsub%2Fproject"
    assert git_host.calls() == [
        f"GET {project}/repository/compare?from=develop&to=feature%2Fx",
        f"GET {project}/merge_requests?source_branch=feature%2Fx&state=opened",
        f"POST {project}/merge_requests",
    ]
    body = json.loads(git_host.requests[-1].content)
    assert body["source_branch"] == "feature/x" and body["target_branch"] == "develop"
    assert body["title"] == "Add a /healthz route"


async def test_a_git_host_ends_at_the_branch(manager, git_host) -> None:
    task = await develop(manager, GIT_REPO, branch="feature/x")
    row = await manager.record_report(task["task_id"], pushed())
    assert row is not None
    assert row["status"] == "success"
    assert row["scan"] == "unavailable"
    assert row["branch_name"] == "feature/x"
    assert row["pr_url"] is None
    assert git_host.requests == []


async def test_a_push_with_files_without_patch_is_partial(manager, git_host) -> None:
    git_host.compare_files = [clean_file(), {"filename": "blob.bin", "additions": 3}]
    task = await develop(manager)
    row = await manager.record_report(task["task_id"], pushed())
    assert row is not None and row["scan"] == "partial" and row["status"] == "success"


async def test_a_report_without_a_push_touches_no_host(manager, git_host) -> None:
    task = await develop(manager)
    row = await manager.record_report(task["task_id"], pushed(pushed=False))
    assert row is not None and row["status"] == "success" and row["scan"] is None
    assert git_host.requests == []


async def test_a_failed_push_is_scanned_and_gets_no_pr(manager, git_host) -> None:
    task = await develop(manager)
    row = await manager.record_report(task["task_id"], pushed(status="timed_out"))
    assert row is not None
    assert (row["status"], row["scan"], row["pr_url"]) == ("timed_out", "clean", None)
    assert len(git_host.requests) == 1


async def test_a_push_to_another_branch_fails_without_a_call(manager, git_host) -> None:
    task = await develop(manager, branch="feature/x")
    row = await manager.record_report(task["task_id"], pushed(head="main"))
    assert row is not None
    assert row["status"] == "failed"
    assert "not feature/x" in row["error"]
    assert row["branch_name"] == "feature/x"
    assert git_host.requests == []


async def test_a_host_error_fails_the_task_and_keeps_the_branch(manager, git_host) -> None:
    token = GIT_TOKENS["GITHUB_TOKEN_ALEX"]
    git_host.override(
        "POST", r"/pulls$", httpx.Response(422, json={"message": f"Validation Failed {token}"})
    )
    task = await develop(manager, branch="feature/x")
    row = await manager.record_report(task["task_id"], pushed())
    assert row is not None
    assert row["status"] == "failed"
    assert row["error"] == (
        "the git host refused the call for the pull request "
        "(HTTP 422: Validation Failed [redacted]); the branch feature/x stays"
    )
    assert git_host.calls("DELETE") == []
    assert token not in json.dumps(row)


async def test_a_5xx_is_tried_one_more_time(manager, git_host) -> None:
    replies = [httpx.Response(502, json={"message": "bad gateway"})]

    def flaky(request: httpx.Request) -> httpx.Response:
        return replies.pop() if replies else httpx.Response(200, json={"files": []})

    git_host.override("GET", r"/compare/", flaky)
    task = await develop(manager)
    row = await manager.record_report(task["task_id"], pushed())
    assert row is not None and row["status"] == "success"
    assert len([c for c in git_host.calls() if "/compare/" in c]) == 2


async def test_two_5xx_fail_the_task(manager, git_host) -> None:
    git_host.override("GET", r"/compare/", httpx.Response(500, json={"message": "boom"}))
    task = await develop(manager)
    row = await manager.record_report(task["task_id"], pushed())
    assert row is not None
    assert row["status"] == "failed"
    assert "the diff (HTTP 500: boom)" in row["error"]
    assert len(git_host.requests) == 2


async def test_retry_5xx_does_not_retry_a_4xx() -> None:
    calls: list[int] = []

    async def call() -> None:
        calls.append(1)
        raise PlatformError(404, "gone")

    with pytest.raises(PlatformError):
        await retry_5xx(call)
    assert calls == [1]


async def test_a_rework_push_confirms_the_pr(manager, git_host) -> None:
    result = await manager.rework("alex", REPO, 5, "x")
    row = await manager.record_report(result["task_id"], pushed(head="pr-5"))
    assert row is not None
    assert row["status"] == "success"
    assert row["pr_number"] == 5
    assert git_host.calls() == [
        "GET /repos/example-home/app/pulls/5",
        "GET /repos/example-home/app/compare/main...pr-5",
        "GET /repos/example-home/app/pulls/5",
    ]


async def test_a_rework_push_whose_pr_is_gone(manager, git_host) -> None:
    result = await manager.rework("alex", REPO, 5, "x")
    git_host.override("GET", r"/pulls/5$", httpx.Response(404, json={"message": "Not Found"}))
    row = await manager.record_report(result["task_id"], pushed())
    assert row is not None
    assert row["status"] == "failed"
    assert row["error"] == "the pull request 5 is gone; the branch pr-5 stays"


async def test_a_rework_push_when_the_pr_lookup_fails(manager, git_host) -> None:
    result = await manager.rework("alex", REPO, 5, "x")
    git_host.override("GET", r"/pulls/5$", httpx.Response(401, json={"message": "Bad creds"}))
    row = await manager.record_report(result["task_id"], pushed())
    assert row is not None and row["status"] == "failed"
    assert "HTTP 401: Bad creds" in row["error"]


async def test_a_token_gone_before_the_report_fails_the_task(manager, monkeypatch) -> None:
    task = await develop(manager)
    monkeypatch.delenv("GITHUB_TOKEN_ALEX")
    row = await manager.record_report(task["task_id"], pushed())
    assert row is not None and row["status"] == "failed"
    assert "GITHUB_TOKEN_ALEX" in row["error"]


async def test_a_second_report_during_the_gate_is_ignored(manager, git_host) -> None:
    task = await develop(manager)
    manager._finishing.add(task["task_id"])
    row = await manager.record_report(task["task_id"], pushed())
    manager._finishing.discard(task["task_id"])
    assert row is not None and row["status"] == "running"
    assert git_host.requests == []


def test_finding_error_counts_the_other_findings() -> None:
    findings = [Finding("a.py", 3, "github_token"), Finding("b.py", 9, "aws_access_key")]
    assert finding_error(findings) == (
        "the diff holds what looks like a credential (github_token in a.py:3), "
        "and 1 more finding(s)"
    )
