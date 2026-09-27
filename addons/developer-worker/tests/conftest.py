"""Shared fixtures: a fake manager, local git repositories, a fake SDK client.

Every test is offline. ``no_network`` refuses any IP connection, so a test
that tries to reach a host fails. Host names in tests end in ``.test``.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from joshua_developer_worker.manager_client import ManagerClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

TASK_ID = "task-0001"
TASK_TOKEN = "task-token-abc123"
GIT_TOKEN = "git-token-SECRET-987"
MANAGER_URL = "http://manager.test:8001"
PROXY_URL = f"http://task:{TASK_TOKEN}@manager.test:8002"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse every IPv4 and IPv6 connection. Local sockets stay open."""
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: Any) -> Any:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise OSError(f"network access in a test: {address!r}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh ``$HOME``, so git reads no config of the person who runs the tests."""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    return path


def run_git(*args: str, cwd: Path | None = None) -> str:
    """Run git with a fixed identity, for test setup."""
    done = subprocess.run(
        ["git", "-c", "user.name=Seed", "-c", "user.email=seed@example.test", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    """A bare repository with ``main`` (one commit) and ``feature`` (one more)."""
    bare = tmp_path / "remote.git"
    run_git("init", "--quiet", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    run_git("clone", "--quiet", str(bare), str(seed))
    (seed / "README.md").write_text("# sample\n")
    run_git("add", "README.md", cwd=seed)
    run_git("commit", "--quiet", "-m", "initial", cwd=seed)
    run_git("push", "--quiet", "origin", "HEAD:main", cwd=seed)
    run_git("checkout", "--quiet", "-b", "feature", cwd=seed)
    (seed / "feature.txt").write_text("feature\n")
    run_git("add", "feature.txt", cwd=seed)
    run_git("commit", "--quiet", "-m", "feature", cwd=seed)
    run_git("push", "--quiet", "origin", "feature", cwd=seed)
    return bare


def remote_head(bare: Path, branch: str) -> str | None:
    """The commit of ``branch`` in ``bare``, or None."""
    done = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=bare,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip() or None


@dataclass
class FakeManager:
    """The worker API of the manager, with state, for one task."""

    brief: dict[str, Any] = field(default_factory=dict)
    brief_status: int = 200
    brief_error: str = "token_missing"
    answer_text: str = "Use port 8080."
    answer_after_polls: int = 1
    report_status: int = 200
    ended: bool = False
    running: bool = False
    reports: list[dict[str, Any]] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    polls: int = 0
    unauthorized: int = 0
    ask_reply: dict[str, Any] = field(default_factory=lambda: {"asked": True, "sent": True})

    def _check(self, request: Request) -> Response | None:
        auth = request.headers.get("authorization")
        if auth != f"Bearer {TASK_TOKEN}":
            self.unauthorized += 1
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if request.headers.get("x-task-id") != TASK_ID:
            return JSONResponse({"error": "not_found"}, status_code=404)
        if self.ended:
            return JSONResponse({"error": "task_ended"}, status_code=409)
        return None

    def app(self) -> Starlette:
        async def brief(request: Request) -> Response:
            if (refused := self._check(request)) is not None:
                return refused
            if self.brief_status != 200:
                return JSONResponse({"error": self.brief_error}, status_code=self.brief_status)
            return JSONResponse(self.brief)

        async def running(request: Request) -> Response:
            if (refused := self._check(request)) is not None:
                return refused
            self.running = True
            return JSONResponse({"status": "running"})

        async def report(request: Request) -> Response:
            if (refused := self._check(request)) is not None:
                return refused
            if self.report_status != 200:
                return JSONResponse({"error": "boom"}, status_code=self.report_status)
            self.reports.append(await request.json())
            self.ended = True
            return JSONResponse({"recorded": True})

        async def ask(request: Request) -> Response:
            if (refused := self._check(request)) is not None:
                return refused
            self.questions.append((await request.json())["question"])
            self.polls = 0
            return JSONResponse(self.ask_reply, status_code=202)

        async def answer(request: Request) -> Response:
            if (refused := self._check(request)) is not None:
                return refused
            self.polls += 1
            if self.questions and self.polls >= self.answer_after_polls:
                return JSONResponse({"answer": self.answer_text})
            await asyncio.sleep(0)
            return Response(status_code=204)

        async def log(request: Request) -> Response:
            if (refused := self._check(request)) is not None:
                return refused
            self.logs.append((await request.json())["text"])
            return JSONResponse({"appended": True})

        return Starlette(
            routes=[
                Route("/worker/brief", brief, methods=["GET"]),
                Route("/worker/running", running, methods=["POST"]),
                Route("/worker/report", report, methods=["POST"]),
                Route("/worker/ask", ask, methods=["POST"]),
                Route("/worker/answer", answer, methods=["GET"]),
                Route("/worker/log", log, methods=["POST"]),
            ]
        )

    def client(self, token: str = TASK_TOKEN) -> ManagerClient:
        return ManagerClient(
            MANAGER_URL,
            TASK_ID,
            token,
            transport=httpx.ASGITransport(app=self.app()),
            backoff_s=0,
        )


def make_brief(bare: Path, **overrides: Any) -> dict[str, Any]:
    """A brief like the manager's ``build_brief``, for ``bare``."""
    brief: dict[str, Any] = {
        "task_id": TASK_ID,
        "task_type": "develop",
        "repo": "git.test/owner/sample",
        "repo_url": str(bare),
        "branch": "developer/task-0001",
        "base_branch": "main",
        "persona": {
            "name": "opus",
            "model": "claude-opus-5",
            "effort": "high",
            "max_turns": 80,
            "timeout_s": 2400,
        },
        "git": {"name": "Alex Example", "email": "alex@example.test"},
        "git_token": GIT_TOKEN,
        "git_username": "x-access-token",
        "platform_kind": "github",
        "brief": "Add a greeting file named hello.txt.",
        "feedback": None,
        "answer": None,
        "note": None,
    }
    brief.update(overrides)
    return brief


@pytest.fixture
def manager() -> FakeManager:
    return FakeManager()


Step = Any  # a message, an exception, or an async callable that returns one or None


class FakeSDKClient:
    """Stands in for ``ClaudeSDKClient``. It plays a script and starts no CLI."""

    def __init__(self, script: list[Step], options: Any) -> None:
        self.script = script
        self.options = options
        self.prompt: str | None = None
        self.connected = False
        self.interrupted = False
        self.disconnected = False

    async def connect(self) -> None:
        self.connected = True

    async def query(self, prompt: str) -> None:
        self.prompt = prompt

    async def receive_response(self) -> Any:
        for item in self.script:
            if callable(item):
                item = await item(self)
            if item is None:
                continue
            if isinstance(item, BaseException):
                raise item
            yield item

    async def interrupt(self) -> None:
        self.interrupted = True

    async def disconnect(self) -> None:
        self.disconnected = True


def fake_factory(
    script: list[Step], made: list[FakeSDKClient] | None = None
) -> Callable[..., FakeSDKClient]:
    """A ``client_factory`` for ``session.run`` that plays ``script``."""

    def factory(options: Any) -> FakeSDKClient:
        client = FakeSDKClient(script, options)
        if made is not None:
            made.append(client)
        return client

    return factory


async def sleep_forever(_: FakeSDKClient) -> None:
    await asyncio.sleep(3600)


AsyncStep = Callable[[FakeSDKClient], Awaitable[Any]]
