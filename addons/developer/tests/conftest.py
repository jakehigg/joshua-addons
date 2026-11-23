"""Shared fixtures for the developer addon tests.

Every test works on a database in a temporary directory and drives the ASGI
app in-process: no network, no Docker, no worker.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from joshua_developer.config import Caller, Settings, parse_config
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.types import ASGIApp

ALEX = "alex-token"
MIA = "mia-token"
ADDON = "addon-token"

CONFIG: dict[str, Any] = {
    "network": "off",
    # The archive check makes one more call at dispatch. Its own tests turn
    # it on; the others count the calls of the fake git host without it.
    "refuse_archived": False,
    "default_persona": "opus",
    "max_workers": 2,
    "platforms": {
        "github.com": {"kind": "github", "token_env": "GITHUB_TOKEN"},
        "gitlab.example.net": {"kind": "gitlab", "token_env": "GITLAB_TOKEN"},
    },
    "repos": ["github.com/example-home/*"],
    "people": {
        "alex": {
            "git": {"name": "Alex Example", "email": "alex@users.noreply.github.com"},
            "default_persona": "sonnet",
            "notify": "telegram:dm:alex",
            "platforms": {"github.com": {"token_env": "GITHUB_TOKEN_ALEX"}},
            "repos": ["github.com/alex-example/*"],
        },
        "mia": {},
    },
}


# Fake git tokens. No test uses a real credential.
GIT_TOKENS = {
    "GITHUB_TOKEN": "fake-github-instance-token",
    "GITHUB_TOKEN_ALEX": "fake-github-alex-token",
    "GITLAB_TOKEN": "fake-gitlab-token",
}

Handler = Callable[[httpx.Request], httpx.Response]


class FakeGitHost:
    """A fake GitHub and GitLab API, for every adapter client the tests make.

    Each request is recorded. ``override`` puts a reply in front of the
    default reply for a method and a path pattern. By default a pull request
    lookup finds an open pull request with the source branch ``pr-<n>``, and
    the compare is empty. The fake answers no other call, so a call that
    opens a pull request gets a 404.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.overrides: list[tuple[str, re.Pattern[str], Handler]] = []
        self.compare_files: list[dict[str, Any]] = []

    def override(self, method: str, pattern: str, reply: Handler | httpx.Response) -> None:
        handler = reply if callable(reply) else (lambda request, r=reply: r)
        self.overrides.insert(0, (method, re.compile(pattern), handler))

    def calls(self, method: str | None = None) -> list[str]:
        return [
            f"{r.method} {r.url.raw_path.decode()}"
            for r in self.requests
            if method is None or r.method == method
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.raw_path.decode().split("?")[0]
        for method, pattern, handler in self.overrides:
            if method == request.method and pattern.search(path):
                return handler(request)
        gitlab = "/api/v4/" in path
        if request.method == "GET" and re.search(r"/(pulls|merge_requests)/[0-9]+$", path):
            number = int(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=_pr(gitlab, number, f"pr-{number}", "main"))
        if request.method == "GET" and "/compare" in path:
            if gitlab:
                return httpx.Response(200, json={"diffs": self.compare_files})
            return httpx.Response(200, json={"files": self.compare_files})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404, json={"message": "Not Found"})


def _pr(gitlab: bool, number: int, head: str, base: str, state: str | None = None) -> dict:
    if gitlab:
        return {
            "iid": number,
            "project_id": 7,
            "source_project_id": 7,
            "target_project_id": 7,
            "web_url": f"https://gitlab.example.net/g/p/-/merge_requests/{number}",
            "source_branch": head,
            "target_branch": base,
            "state": state or "opened",
        }
    return {
        "number": number,
        "url": f"https://api.github.com/repos/example-home/app/pulls/{number}",
        "html_url": f"https://github.com/example-home/app/pull/{number}",
        "head": {"ref": head},
        "base": {"ref": base},
        "state": state or "open",
    }


def github_pr(number: int, head: str, base: str = "main", state: str = "open") -> dict:
    return _pr(False, number, head, base, state)


def gitlab_mr(number: int, head: str, base: str = "main", state: str = "opened") -> dict:
    return _pr(True, number, head, base, state)


@pytest.fixture(autouse=True)
def git_host(monkeypatch) -> FakeGitHost:
    """Fake git tokens in the environment, and a fake API behind every adapter client."""
    from joshua_developer import platforms

    for name, value in GIT_TOKENS.items():
        monkeypatch.setenv(name, value)
    host = FakeGitHost()
    monkeypatch.setattr(
        platforms,
        "make_client",
        lambda verify=True: httpx.AsyncClient(transport=httpx.MockTransport(host.handler)),
    )
    return host


# --- the credential scan ---------------------------------------------------

# The gitleaks binary is not in the test environment, so every test runs the
# scan against this stand-in, and never a real scanner. The stand-in answers
# with a recorded JSON report, which holds a rule name, a file, and a line,
# and no value. By default it reports one finding for each line that holds a
# run of 30 characters or more without a space, which is the shape of the
# fake credentials of the tests.
LEAK_RE = re.compile(r"\S{30,}")
LEAK_RULE = "github-pat"


class FakeGitleaks:
    """The gitleaks binary of the tests.

    ``calls`` holds the arguments of each run, and ``written`` the text of
    each file the scan wrote, by the path of the file under the directory
    that the scan gives to gitleaks. ``report`` puts a recorded report in
    front of the default report; the ``File`` of an item of it is relative to
    that directory too. ``report_text`` writes a report that is not a list of
    findings, and ``returncode`` and ``error`` make a run fail.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.written: dict[str, str] = {}
        self.report: list[dict[str, Any]] | None = None
        self.report_text: str | None = None
        self.returncode = 0
        self.error: Exception | None = None

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if self.error is not None:
            raise self.error
        target = Path(args[2])
        self.written = {
            str(path.relative_to(target)): path.read_text(encoding="utf-8")
            for path in sorted(target.rglob("*"))
            if path.is_file()
        }
        report = Path(args[args.index("--report-path") + 1])
        if self.report_text is not None:
            report.write_text(self.report_text, encoding="utf-8")
        else:
            items = self.default_report() if self.report is None else self.report
            report.write_text(
                json.dumps([{**item, "File": str(target / str(item["File"]))} for item in items]),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(args, self.returncode, "", "")

    def default_report(self) -> list[dict[str, Any]]:
        """One finding for each line of a written file that looks like a credential."""
        return [
            {"RuleID": LEAK_RULE, "File": file, "StartLine": line_no}
            for file, text in self.written.items()
            for line_no, line in enumerate(text.splitlines(), start=1)
            if LEAK_RE.search(line)
        ]


@pytest.fixture(autouse=True)
def gitleaks(monkeypatch) -> FakeGitleaks:
    """The scan of every test runs the fake gitleaks, and never a binary."""
    from joshua_developer import scan

    fake = FakeGitleaks()
    monkeypatch.setattr(scan, "run_gitleaks", fake.run)
    real_which = shutil.which
    monkeypatch.setattr(
        scan.shutil,
        "which",
        lambda name, *args, **kw: (
            f"/usr/bin/{scan.GITLEAKS}" if name == scan.GITLEAKS else real_which(name, *args, **kw)
        ),
    )
    return fake


def make_settings(data_dir: Path, **overrides: Any) -> Settings:
    config = json.loads(json.dumps(CONFIG))
    config.update(overrides.pop("config", {}))
    fields: dict[str, Any] = {
        "config": parse_config(config),
        "data_dir": data_dir,
        "tokens": {
            ALEX: Caller(person="alex"),
            MIA: Caller(person="mia"),
            ADDON: Caller(person=None),
        },
    }
    fields.update(overrides)
    return Settings(**fields)


@pytest.fixture(autouse=True)
def close_the_manager():
    """Close the store of the app a test built, so no database stays open."""
    yield
    from joshua_developer import server

    if server._manager is not None:
        server._manager.store.close()
        server._manager = None


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def settings(data_dir: Path) -> Settings:
    return make_settings(data_dir)


@pytest.fixture
def app(settings: Settings) -> ASGIApp:
    """The app with a stub runtime that records its report at once."""
    from joshua_developer.server import build_app

    return build_app(settings, stub_delay_s=0)


@pytest.fixture
def holding_app(settings: Settings) -> ASGIApp:
    """The app with a stub runtime that never records a report: tasks stay running."""
    from joshua_developer.server import build_app

    return build_app(settings, stub_delay_s=None)


def payload(result) -> dict:
    assert result.is_error is not True, result.content
    [block] = result.content
    return json.loads(block.text)


def error(result) -> str:
    assert result.is_error is True
    return result.content[0].text


@contextlib.asynccontextmanager
async def _client_session(app: ASGIApp, token: str | None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    transport = httpx2.ASGITransport(app=app)
    client = httpx2.AsyncClient(
        transport=transport, base_url="http://testserver", headers=headers, timeout=10
    )
    try:
        async with streamable_http_client("http://testserver/mcp", http_client=client) as (
            read,
            write_stream,
        ):
            async with ClientSession(read, write_stream) as session:
                await session.initialize()
                yield session
    finally:
        await client.aclose()


@contextlib.asynccontextmanager
async def mcp_sessions(app: ASGIApp, *tokens: str | None):
    """Open one MCP session for each token to ``app``, over an in-process ASGI transport.

    An ASGI transport never sends the ``lifespan`` protocol, so enter the
    session manager by hand, as the other addon tests do. The session
    manager runs once for each app, so every session of a test opens here.
    """
    from joshua_developer.server import mcp as addon_mcp

    async with addon_mcp.session_manager.run():
        async with contextlib.AsyncExitStack() as stack:
            sessions = [await stack.enter_async_context(_client_session(app, t)) for t in tokens]
            yield sessions


@contextlib.asynccontextmanager
async def mcp_session(app: ASGIApp, token: str | None = ALEX):
    """Open one MCP session to ``app`` with ``token``."""
    async with mcp_sessions(app, token) as (session,):
        yield session
