"""Shared fixtures for the developer addon tests.

Every test works on a database in a temporary directory and drives the ASGI
app in-process: no network, no Docker, no worker.
"""

from __future__ import annotations

import contextlib
import json
import re
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
    lookup finds an open pull request with the source branch ``pr-<n>``, no
    open pull request exists for a branch, the compare is empty, and opening
    a pull request gives number 42.
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
        if request.method == "GET" and re.search(r"/(pulls|merge_requests)$", path):
            return httpx.Response(200, json=[])
        if request.method == "POST" and re.search(r"/(pulls|merge_requests)$", path):
            body = json.loads(request.content)
            head = body.get("head") or body.get("source_branch")
            base = body.get("base") or body.get("target_branch")
            return httpx.Response(201, json=_pr(gitlab, 42, head, base))
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
