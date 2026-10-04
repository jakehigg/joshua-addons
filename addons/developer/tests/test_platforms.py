"""The git host adapters against a mocked HTTP client, and the resolution of a
platform entry and its token. Every test is offline."""

from __future__ import annotations

import json
from collections.abc import Callable

import certifi
import httpx
import pytest
from conftest import make_settings
from joshua_developer import platforms
from joshua_developer.config import parse_config
from joshua_developer.platforms import (
    Diff,
    GitHubPlatform,
    GitLabPlatform,
    GitPlatform,
    PlatformError,
    PlatformUnsupported,
    RepoNotConfigured,
    TokenMissing,
    count_lines,
    for_task,
    resolve,
    scrub,
)
from joshua_developer.platforms import (
    make_client as real_make_client,
)

TOKEN = "fake-platform-token-0123456789"
GH_REPO = "github.com/example-home/app"
GL_REPO = "gitlab.example.net/group/sub/project"


class Recorder:
    """A MockTransport handler that records each request and sends one reply."""

    def __init__(self, reply: Callable[[httpx.Request], httpx.Response]) -> None:
        self.reply = reply
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.reply(request)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


def client_for(recorder: Recorder) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(recorder))


def json_reply(status: int, body: object) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(status, json=body)


GH_PR = {
    "number": 7,
    "url": "https://api.github.com/repos/example-home/app/pulls/7",
    "html_url": "https://github.com/example-home/app/pull/7",
    "head": {"ref": "joshua/fix-7"},
    "base": {"ref": "main"},
    "state": "open",
}
GL_MR = {
    "iid": 3,
    "project_id": 11,
    "source_project_id": 11,
    "target_project_id": 11,
    "web_url": "https://gitlab.example.net/group/sub/project/-/merge_requests/3",
    "source_branch": "joshua/fix-3",
    "target_branch": "develop",
    "state": "opened",
}


def assert_github_headers(request: httpx.Request) -> None:
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["accept"] == "application/vnd.github+json"
    assert request.headers["user-agent"] == "joshua-developer"


def assert_gitlab_headers(request: httpx.Request) -> None:
    assert request.headers["private-token"] == TOKEN
    assert "authorization" not in request.headers


# --- github ----------------------------------------------------------------


async def test_github_open_pr() -> None:
    recorder = Recorder(json_reply(201, GH_PR))
    platform = GitHubPlatform("github.com", TOKEN, client=client_for(recorder))
    found = await platform.open_pr(GH_REPO, "joshua/fix-7", "main", "Fix it", "Body")
    request = recorder.last
    assert request.method == "POST"
    assert str(request.url) == "https://api.github.com/repos/example-home/app/pulls"
    assert json.loads(request.content) == {
        "title": "Fix it",
        "head": "joshua/fix-7",
        "base": "main",
        "body": "Body",
    }
    assert_github_headers(request)
    assert found.number == 7
    assert found.html_url == "https://github.com/example-home/app/pull/7"
    assert found.url.startswith("https://api.github.com/")


async def test_github_find_pr() -> None:
    recorder = Recorder(json_reply(200, [GH_PR]))
    platform = GitHubPlatform("github.com", TOKEN, client=client_for(recorder))
    found = await platform.find_pr(GH_REPO, "joshua/fix-7")
    request = recorder.last
    assert request.method == "GET"
    assert request.url.path == "/repos/example-home/app/pulls"
    assert request.url.params["head"] == "example-home:joshua/fix-7"
    assert request.url.params["state"] == "open"
    assert_github_headers(request)
    assert found is not None and found.number == 7

    none = GitHubPlatform("github.com", TOKEN, client=client_for(Recorder(json_reply(200, []))))
    assert await none.find_pr(GH_REPO, "other") is None


async def test_github_pr() -> None:
    recorder = Recorder(json_reply(200, GH_PR))
    platform = GitHubPlatform("github.com", TOKEN, client=client_for(recorder))
    found = await platform.pr(GH_REPO, 7)
    assert recorder.last.method == "GET"
    assert str(recorder.last.url) == "https://api.github.com/repos/example-home/app/pulls/7"
    assert_github_headers(recorder.last)
    assert (found.source_branch, found.base_branch, found.state) == ("joshua/fix-7", "main", "open")


async def test_github_compare() -> None:
    body = {
        "files": [
            {"filename": "a.py", "patch": "@@ -1 +1,2 @@\n x\n+y", "additions": 1, "deletions": 0},
            {"filename": "logo.png", "status": "added", "additions": 1},
            {"filename": "gone.txt", "status": "removed", "additions": 0, "deletions": 4},
        ]
    }
    recorder = Recorder(json_reply(200, body))
    platform = GitHubPlatform("github.com", TOKEN, client=client_for(recorder))
    diff = await platform.compare(GH_REPO, "main", "joshua/fix-7")
    assert recorder.last.method == "GET"
    assert recorder.last.url.raw_path.decode() == (
        "/repos/example-home/app/compare/main...joshua/fix-7?per_page=100&page=1"
    )
    assert_github_headers(recorder.last)
    assert [f.path for f in diff.files] == ["a.py"]
    assert diff.files[0].patch.endswith("+y")
    assert (diff.additions, diff.deletions) == (2, 4)
    assert diff.unscanned == ["logo.png"]
    assert diff.unavailable is False
    assert diff.truncated is False


def compare_file(index: int) -> dict:
    return {"filename": f"f{index}.py", "patch": "@@ -0,0 +1 @@\n+x", "additions": 1}


async def test_github_compare_reads_every_page() -> None:
    pages = {
        "1": {"files": [compare_file(i) for i in range(100)]},
        "2": {"files": [compare_file(i) for i in range(100, 150)]},
    }
    seen: list[str] = []

    def reply(request: httpx.Request) -> httpx.Response:
        assert request.url.params["per_page"] == "100"
        seen.append(request.url.params["page"])
        return httpx.Response(200, json=pages[request.url.params["page"]])

    platform = GitHubPlatform("github.com", TOKEN, client=client_for(Recorder(reply)))
    diff = await platform.compare(GH_REPO, "main", "joshua/fix-7")
    assert seen == ["1", "2"]
    assert len(diff.files) == 150 and diff.additions == 150
    assert diff.truncated is False


async def test_github_compare_at_the_file_cap_is_truncated() -> None:
    # The host lists 300 files on each page and pages no further: files can be missing.
    capped = {"files": [compare_file(i) for i in range(300)]}
    seen: list[str] = []

    def reply(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["page"])
        return httpx.Response(200, json=capped if request.url.params["page"] == "1" else {})

    platform = GitHubPlatform("github.com", TOKEN, client=client_for(Recorder(reply)))
    diff = await platform.compare(GH_REPO, "main", "joshua/fix-7")
    assert seen == ["1", "2"]
    assert len(diff.files) == 300
    assert diff.truncated is True

    again = GitHubPlatform(
        "github.com", TOKEN, client=client_for(Recorder(json_reply(200, capped)))
    )
    assert (await again.compare(GH_REPO, "main", "x")).truncated is True


async def test_github_compare_stops_at_the_page_limit(monkeypatch) -> None:
    from joshua_developer import platforms

    monkeypatch.setattr(platforms, "GITHUB_MAX_PAGES", 2)
    count = iter(range(10_000))

    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"files": [compare_file(next(count)) for _ in range(100)]})

    platform = GitHubPlatform("github.com", TOKEN, client=client_for(Recorder(reply)))
    diff = await platform.compare(GH_REPO, "main", "x")
    assert len(diff.files) == 200
    assert diff.truncated is True


async def test_github_delete_branch() -> None:
    recorder = Recorder(lambda request: httpx.Response(204))
    platform = GitHubPlatform("github.com", TOKEN, client=client_for(recorder))
    await platform.delete_branch(GH_REPO, "joshua/fix-7")
    assert recorder.last.method == "DELETE"
    assert recorder.last.url.raw_path.decode() == (
        "/repos/example-home/app/git/refs/heads/joshua/fix-7"
    )
    assert_github_headers(recorder.last)


async def test_github_enterprise_uses_the_api_v3_base() -> None:
    recorder = Recorder(json_reply(200, GH_PR))
    platform = GitHubPlatform("ghe.example.net", TOKEN, client=client_for(recorder))
    await platform.pr("ghe.example.net/team/app", 7)
    assert str(recorder.last.url) == "https://ghe.example.net/api/v3/repos/team/app/pulls/7"
    assert platform.credential_username() == "x-access-token"


# --- gitlab ----------------------------------------------------------------


async def test_gitlab_open_pr_with_a_subgroup() -> None:
    recorder = Recorder(json_reply(201, GL_MR))
    platform = GitLabPlatform("gitlab.example.net", TOKEN, client=client_for(recorder))
    found = await platform.open_pr(GL_REPO, "joshua/fix-3", "develop", "Fix it", "Body")
    request = recorder.last
    assert request.method == "POST"
    assert request.url.raw_path.decode() == (
        "/api/v4/projects/group%2Fsub%2Fproject/merge_requests"
    )
    assert request.url.host == "gitlab.example.net"
    assert json.loads(request.content) == {
        "source_branch": "joshua/fix-3",
        "target_branch": "develop",
        "title": "Fix it",
        "description": "Body",
    }
    assert_gitlab_headers(request)
    assert found.number == 3
    assert found.html_url.endswith("/merge_requests/3")


async def test_gitlab_find_pr() -> None:
    recorder = Recorder(json_reply(200, [GL_MR]))
    platform = GitLabPlatform("gitlab.example.net", TOKEN, client=client_for(recorder))
    found = await platform.find_pr(GL_REPO, "joshua/fix-3")
    request = recorder.last
    assert request.method == "GET"
    assert request.url.raw_path.decode().split("?")[0] == (
        "/api/v4/projects/group%2Fsub%2Fproject/merge_requests"
    )
    assert request.url.params["source_branch"] == "joshua/fix-3"
    assert request.url.params["state"] == "opened"
    assert_gitlab_headers(request)
    assert found is not None and found.number == 3

    none = GitLabPlatform(
        "gitlab.example.net", TOKEN, client=client_for(Recorder(json_reply(200, [])))
    )
    assert await none.find_pr(GL_REPO, "x") is None


async def test_gitlab_find_pr_skips_a_merge_request_from_a_fork() -> None:
    fork = {**GL_MR, "iid": 9, "source_project_id": 99}
    unknown = {**GL_MR, "iid": 10, "source_project_id": None}
    recorder = Recorder(json_reply(200, [fork, unknown, GL_MR]))
    platform = GitLabPlatform("gitlab.example.net", TOKEN, client=client_for(recorder))
    found = await platform.find_pr(GL_REPO, "joshua/fix-3")
    assert found is not None and found.number == 3
    only_fork = GitLabPlatform(
        "gitlab.example.net", TOKEN, client=client_for(Recorder(json_reply(200, [fork])))
    )
    assert await only_fork.find_pr(GL_REPO, "joshua/fix-3") is None
    # project_id stands in when target_project_id is missing.
    old = {k: v for k, v in GL_MR.items() if k != "target_project_id"}
    older = GitLabPlatform(
        "gitlab.example.net", TOKEN, client=client_for(Recorder(json_reply(200, [old])))
    )
    assert (await older.find_pr(GL_REPO, "joshua/fix-3")) is not None


async def test_gitlab_pr() -> None:
    recorder = Recorder(json_reply(200, GL_MR))
    platform = GitLabPlatform("gitlab.example.net", TOKEN, client=client_for(recorder))
    found = await platform.pr(GL_REPO, 3)
    assert recorder.last.method == "GET"
    assert recorder.last.url.raw_path.decode() == (
        "/api/v4/projects/group%2Fsub%2Fproject/merge_requests/3"
    )
    assert_gitlab_headers(recorder.last)
    assert (found.source_branch, found.base_branch, found.state) == (
        "joshua/fix-3",
        "develop",
        "opened",
    )


async def test_gitlab_compare() -> None:
    body = {
        "diffs": [
            {"new_path": "a.py", "diff": "@@ -1,2 +1,2 @@\n-x\n+y\n z"},
            {"new_path": "big.bin", "diff": ""},
            {"old_path": "gone.txt", "new_path": "gone.txt", "diff": "", "deleted_file": True},
        ]
    }
    recorder = Recorder(json_reply(200, body))
    platform = GitLabPlatform("gitlab.example.net", TOKEN, client=client_for(recorder))
    diff = await platform.compare(GL_REPO, "develop", "joshua/fix-3")
    request = recorder.last
    assert request.method == "GET"
    assert request.url.raw_path.decode().split("?")[0] == (
        "/api/v4/projects/group%2Fsub%2Fproject/repository/compare"
    )
    assert request.url.params["from"] == "develop"
    assert request.url.params["to"] == "joshua/fix-3"
    assert_gitlab_headers(request)
    assert [f.path for f in diff.files] == ["a.py"]
    assert (diff.additions, diff.deletions) == (1, 1)
    assert diff.unscanned == ["big.bin"]


async def test_gitlab_delete_branch_encodes_the_slash() -> None:
    recorder = Recorder(lambda request: httpx.Response(204))
    platform = GitLabPlatform("gitlab.example.net", TOKEN, client=client_for(recorder))
    await platform.delete_branch(GL_REPO, "joshua/fix-3")
    assert recorder.last.method == "DELETE"
    assert recorder.last.url.raw_path.decode() == (
        "/api/v4/projects/group%2Fsub%2Fproject/repository/branches/joshua%2Ffix-3"
    )
    assert_gitlab_headers(recorder.last)
    assert platform.credential_username() == "oauth2"


def test_gitlab_project_id() -> None:
    assert GitLabPlatform.project_id("gitlab.example.net/a/b") == "a%2Fb"
    assert GitLabPlatform.project_id("gitlab.example.net:8443/a/b/c/d") == "a%2Fb%2Fc%2Fd"


async def test_gitlab_on_a_port() -> None:
    recorder = Recorder(json_reply(200, GL_MR))
    platform = GitLabPlatform("gitlab.example.net:8443", TOKEN, client=client_for(recorder))
    await platform.pr("gitlab.example.net:8443/g/p", 3)
    assert str(recorder.last.url) == (
        "https://gitlab.example.net:8443/api/v4/projects/g%2Fp/merge_requests/3"
    )


# --- errors ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "repo"),
    [(GitHubPlatform, GH_REPO), (GitLabPlatform, GL_REPO)],
)
async def test_an_error_never_holds_the_token(kind, repo) -> None:
    body = {
        "message": f"Bad credentials for {TOKEN} at https://x-access-token:{TOKEN}@host/p.git",
        "error": f"token {TOKEN}",
    }
    recorder = Recorder(json_reply(401, body))
    host = repo.split("/", 1)[0]
    platform = kind(host, TOKEN, client=client_for(recorder))
    for call in (
        lambda: platform.pr(repo, 1),
        lambda: platform.find_pr(repo, "b"),
        lambda: platform.open_pr(repo, "b", "main", "t", "x"),
        lambda: platform.compare(repo, "main", "b"),
        lambda: platform.delete_branch(repo, "b"),
    ):
        with pytest.raises(PlatformError) as exc:
            await call()
        assert exc.value.status == 401
        text = f"{exc.value} {exc.value.message} {exc.value!r} {exc.value.args}"
        assert TOKEN not in text
        assert "Bad credentials" in text
        assert "[redacted]" in text
        assert "/pulls" not in text and "/api/" not in text
    assert TOKEN not in repr(platform)


async def test_an_error_without_json_uses_the_reason_phrase() -> None:
    recorder = Recorder(lambda request: httpx.Response(502, text="<html>bad gateway</html>"))
    platform = GitHubPlatform("github.com", TOKEN, client=client_for(recorder))
    with pytest.raises(PlatformError) as exc:
        await platform.pr(GH_REPO, 1)
    assert exc.value.status == 502
    assert exc.value.message == "Bad Gateway"


async def test_a_reply_that_is_not_json_is_an_error() -> None:
    recorder = Recorder(lambda request: httpx.Response(200, text="not json"))
    platform = GitHubPlatform("github.com", TOKEN, client=client_for(recorder))
    with pytest.raises(PlatformError) as exc:
        await platform.pr(GH_REPO, 1)
    assert "not JSON" in exc.value.message


async def test_a_host_that_does_not_answer_is_status_0() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach https://oauth2:{TOKEN}@gitlab.example.net")

    platform = GitLabPlatform("gitlab.example.net", TOKEN, client=client_for(Recorder(refuse)))
    with pytest.raises(PlatformError) as exc:
        await platform.pr(GL_REPO, 1)
    assert exc.value.status == 0
    assert str(exc.value) == "the git host gitlab.example.net did not answer (ConnectError)"
    assert TOKEN not in str(exc.value)


def test_scrub_removes_url_credentials_and_secrets() -> None:
    assert scrub("see https://user:pw@h/x and ssh://git@h/y") == (
        "see https://[redacted]@h/x and ssh://[redacted]@h/y"
    )
    assert scrub("a SECRET b", ("SECRET", "")) == "a [redacted] b"


def test_count_lines() -> None:
    assert count_lines("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n+c\n d") == (2, 1)


# --- git -------------------------------------------------------------------


async def test_the_git_kind_refuses_the_pull_request_calls() -> None:
    platform = GitPlatform("git.example.org")
    for call in (
        lambda: platform.open_pr("git.example.org/g/p", "b", "main", "t", "x"),
        lambda: platform.find_pr("git.example.org/g/p", "b"),
        lambda: platform.pr("git.example.org/g/p", 1),
        lambda: platform.delete_branch("git.example.org/g/p", "b"),
    ):
        with pytest.raises(PlatformUnsupported) as exc:
            await call()
        assert "ends at the pushed branch" in str(exc.value)
    diff = await platform.compare("git.example.org/g/p", "main", "b")
    assert diff == Diff(unavailable=True)
    assert platform.credential_username() == "git"
    await platform.aclose()


# --- resolution ------------------------------------------------------------


CONFIG = {
    "platforms": {
        "github.com": {"kind": "github", "token_env": "GH_SHARED"},
        "gitlab.example.net": {"kind": "gitlab", "token_env": "GL_SHARED"},
    },
    "people": {
        "alex": {
            "platforms": {
                "github.com": {"token_env": "GH_ALEX"},
                "git.example.org": {"kind": "git", "token_env": "GIT_ALEX"},
            }
        },
        "mia": {},
    },
}
ENV = {"GH_SHARED": "shared", "GH_ALEX": "alex-own", "GL_SHARED": "gl", "GIT_ALEX": "git-alex"}


def test_resolve_prefers_the_person_then_the_instance() -> None:
    cfg = parse_config(CONFIG)
    own = resolve(cfg, "alex", GH_REPO, ENV)
    shared = resolve(cfg, "mia", GH_REPO, ENV)
    person_only = resolve(cfg, "alex", "git.example.org/g/p", ENV)
    assert (own.kind, own.token_env, own.token) == ("github", "GH_ALEX", "alex-own")
    assert (shared.kind, shared.token_env, shared.token) == ("github", "GH_SHARED", "shared")
    assert (person_only.kind, person_only.token) == ("git", "git-alex")
    assert person_only.credential_username == "git"
    assert "alex-own" not in repr(own)


def test_resolve_refuses_an_unknown_host_by_name() -> None:
    cfg = parse_config(CONFIG)
    with pytest.raises(RepoNotConfigured) as exc:
        resolve(cfg, "mia", "git.example.org/g/p", ENV)
    assert "git.example.org" in str(exc.value)
    assert "github.com" not in str(exc.value) and "gitlab" not in str(exc.value)
    assert exc.value.reason == "repo_not_configured"


@pytest.mark.parametrize("env", [{}, {"GH_SHARED": ""}, {"GH_SHARED": "   "}])
def test_resolve_refuses_a_missing_token_by_variable(env) -> None:
    cfg = parse_config(CONFIG)
    with pytest.raises(TokenMissing) as exc:
        resolve(cfg, "mia", GH_REPO, env)
    assert "GH_SHARED" in str(exc.value)
    assert exc.value.reason == "token_missing"


async def test_for_task_builds_the_adapter_for_the_kind(tmp_path) -> None:
    settings = make_settings(tmp_path)
    cfg = parse_config(CONFIG)
    github, token = for_task(settings, cfg, "alex", GH_REPO, env=ENV)
    gitlab, _ = for_task(settings, cfg, "mia", GL_REPO, env=ENV)
    git, git_token = for_task(settings, cfg, "alex", "git.example.org/g/p", env=ENV)
    assert isinstance(github, GitHubPlatform) and token == "alex-own"
    assert isinstance(gitlab, GitLabPlatform)
    assert isinstance(git, GitPlatform) and git_token == "git-alex"
    for platform in (github, gitlab, git):
        await platform.aclose()


async def test_for_task_reads_the_process_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GH_SHARED", "from-env")
    platform, token = for_task(make_settings(tmp_path), parse_config(CONFIG), "mia", GH_REPO)
    assert token == "from-env"
    await platform.aclose()


async def test_for_task_passes_the_ca_bundle(tmp_path, monkeypatch) -> None:
    seen: list[object] = []

    def fake_client(verify: object = True) -> httpx.AsyncClient:
        seen.append(verify)
        return httpx.AsyncClient()

    monkeypatch.setattr(platforms, "make_client", fake_client)
    cfg = parse_config(CONFIG)
    bundled = make_settings(tmp_path, git_ca_bundle="/etc/ssl/private-ca.pem")
    first, _ = for_task(bundled, cfg, "mia", GH_REPO, env=ENV)
    second, _ = for_task(make_settings(tmp_path), cfg, "mia", GH_REPO, env=ENV)
    assert seen == ["/etc/ssl/private-ca.pem", True]
    await first.aclose()
    await second.aclose()


async def test_make_client_reads_the_bundle() -> None:
    client = real_make_client(certifi.where())
    await client.aclose()
    await real_make_client().aclose()
    with pytest.raises(FileNotFoundError):
        real_make_client("/nonexistent/ca.pem")
