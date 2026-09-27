"""The worker API on port 8001: task tokens, the routes, ask and answer, and the
Claude forwarder. Every test is offline: the Claude API is an httpx MockTransport."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket

import httpx
import pytest
import uvicorn
from conftest import make_settings
from joshua_developer import notify
from joshua_developer.locks import LockManager
from joshua_developer.manager import Manager
from joshua_developer.runtime import StubRuntime
from joshua_developer.store import TaskStore
from joshua_developer.worker_api import build_brief, build_worker_app, repo_url

REPO = "github.com/example-home/app"
CLAUDE = "claude-real-token-value"


@pytest.fixture
def manager(tmp_path):
    settings = make_settings(tmp_path, claude_token=CLAUDE)
    store = TaskStore(settings.db_path)
    manager = Manager(settings, store, LockManager())
    manager.runtime = StubRuntime(manager, delay_s=None)
    yield manager
    store.close()


@pytest.fixture
def sent(monkeypatch) -> list[tuple[str, str]]:
    """The questions sent to channels, as (task id, question)."""
    out: list[tuple[str, str]] = []

    async def fake_question(settings, task, question, default=None) -> bool:
        out.append((task["task_id"], question))
        return True

    monkeypatch.setattr(notify, "send_question", fake_question)
    return out


async def dispatch(manager: Manager, **extra) -> tuple[str, str]:
    """Start a task. Returns its id and its task token."""
    result = await manager.develop("alex", REPO, "Add a health check.", **extra)
    task_id = result["task_id"]
    full = manager.store.get_task_full(task_id)
    assert full is not None and full["worker_token"]
    return task_id, full["worker_token"]


class Upstream:
    """A fake Claude API that records the requests it gets."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(await request.aread())
        # A body from an async iterator stays unread, as with a real connection.
        return httpx.Response(
            200, headers={"content-type": "application/json"}, content=_once(MESSAGE)
        )


MESSAGE = json.dumps({"id": "msg_1", "type": "message"}).encode()


async def _once(data: bytes):
    yield data


@contextlib.asynccontextmanager
async def worker_client(manager: Manager, token: str | None, **app_args):
    app = build_worker_app(manager, **app_args)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://developer:8001", headers=headers
    ) as client:
        yield client


ROUTES = [
    ("GET", "/worker/brief", None),
    ("POST", "/worker/running", None),
    ("POST", "/worker/report", {"status": "success"}),
    ("POST", "/worker/ask", {"question": "q?"}),
    ("GET", "/worker/answer", None),
    ("POST", "/worker/log", {"text": "line"}),
    ("POST", "/worker/claude/v1/messages", {"model": "m"}),
]


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
async def test_every_route_refuses_a_missing_or_wrong_token(manager, method, path, body) -> None:
    await dispatch(manager)
    for token in (None, "wrong-token", ""):
        async with worker_client(manager, token) as client:
            response = await client.request(method, path, json=body)
        assert response.status_code == 401
        assert response.json() == {"error": "unauthorized"}


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
async def test_a_token_for_another_task_gets_404(manager, method, path, body) -> None:
    first, token = await dispatch(manager, branch="one")
    second, _ = await dispatch(manager, branch="two")
    async with worker_client(manager, token) as client:
        response = await client.request(method, path, json=body, headers={"X-Task-Id": second})
    assert response.status_code == 404


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
async def test_a_task_that_ended_gets_409(manager, method, path, body) -> None:
    task_id, token = await dispatch(manager)
    manager.store.update_task(task_id, status="failed")
    async with worker_client(manager, token) as client:
        response = await client.request(method, path, json=body)
    assert response.status_code == 409
    assert response.json() == {"error": "task_ended"}


async def test_healthz_is_open(manager) -> None:
    async with worker_client(manager, None) as client:
        response = await client.get("/healthz")
    assert response.json() == {"ok": True}


async def test_the_brief_has_what_the_worker_needs(manager) -> None:
    task_id, token = await dispatch(manager, branch="feature", base_branch="main", persona="opus")
    async with worker_client(manager, token) as client:
        response = await client.get("/worker/brief", headers={"X-Task-Id": task_id})
    brief = response.json()
    assert response.status_code == 200
    assert brief == {
        "task_id": task_id,
        "task_type": "develop",
        "repo": REPO,
        "repo_url": "https://github.com/example-home/app.git",
        "branch": "feature",
        "base_branch": "main",
        "persona": {
            "name": "opus",
            "model": "claude-opus-5",
            "effort": "high",
            "max_turns": 80,
            "timeout_s": 2400,
        },
        "git": {"name": "Alex Example", "email": "alex@users.noreply.github.com"},
        "git_token": None,
        "brief": "Add a health check.",
        "feedback": None,
        "answer": None,
        "note": None,
    }
    assert token not in response.text


async def test_the_brief_of_a_rework_and_of_a_persona_that_is_gone(manager) -> None:
    result = await manager.rework("alex", REPO, 5, "Rename the flag.")
    full = manager.store.get_task_full(result["task_id"])
    assert full is not None
    brief = build_brief(manager, {**full, "persona": "retired", "report": {"summary": "half"}})
    assert brief["task_type"] == "rework"
    assert brief["feedback"] == "Rename the flag."
    assert brief["persona"]["name"] == "opus"
    assert brief["note"] == "half"
    assert repo_url("gitlab.example.net:8443/g/s/p") == "https://gitlab.example.net:8443/g/s/p.git"


async def test_running_then_report_ends_the_task(manager) -> None:
    task_id, token = await dispatch(manager)
    async with worker_client(manager, token) as client:
        running = await client.post("/worker/running")
        bad = await client.post("/worker/report", json={"status": "done"})
        not_json = await client.post("/worker/report", content=b"{")
        report = await client.post(
            "/worker/report", json={"status": "success", "summary": "ok", "branch": "b"}
        )
        again = await client.post("/worker/report", json={"status": "success"})
    assert running.json() == {"status": "running"}
    assert bad.status_code == 400
    assert not_json.status_code == 400
    assert report.status_code == 200
    assert report.json() == {"recorded": True}
    assert again.status_code == 409
    task = manager.store.get_task(task_id)
    assert task is not None and task["status"] == "success" and task["summary"] == "ok"


async def test_ask_then_answer_wakes_the_long_poll(manager, sent) -> None:
    task_id, token = await dispatch(manager)
    async with worker_client(manager, token, poll_s=5) as client:
        empty = await client.post("/worker/ask", json={"question": " "})
        too_long = await client.post("/worker/ask", json={"question": "x" * 10_001})
        asked = await client.post("/worker/ask", json={"question": "Which database?"})
        assert asked.status_code == 202
        assert asked.json() == {"asked": True, "sent": True}
        assert sent == [(task_id, "Which database?")]
        task = manager.store.get_task(task_id)
        assert task is not None and task["open_question"] == "Which database?"

        waiting = asyncio.create_task(client.get("/worker/answer"))
        await asyncio.sleep(0.05)
        assert not waiting.done()
        result = manager.answer("alex", {"task_id": task_id}, "SQLite")
        answer = await asyncio.wait_for(waiting, 2)
        at_once = await client.get("/worker/answer")
    assert empty.status_code == 400
    assert too_long.status_code == 400
    assert result["status"] == "answered"
    assert answer.status_code == 200
    assert answer.json() == {"answer": "SQLite"}
    assert at_once.json() == {"answer": "SQLite"}


async def test_the_long_poll_gives_204_without_an_answer(manager, sent) -> None:
    _, token = await dispatch(manager)
    async with worker_client(manager, token, poll_s=0.01) as client:
        await client.post("/worker/ask", json={"question": "q?"})
        response = await client.get("/worker/answer")
    assert response.status_code == 204


async def test_a_second_ask_clears_the_old_answer(manager, sent) -> None:
    task_id, token = await dispatch(manager)
    async with worker_client(manager, token, poll_s=0.01) as client:
        await client.post("/worker/ask", json={"question": "first?"})
        manager.answer("alex", {"task_id": task_id}, "one")
        await client.post("/worker/ask", json={"question": "second?"})
        after = await client.get("/worker/answer")
        accepted = manager.answer("alex", {"task_id": task_id}, "two")
        second = await client.get("/worker/answer")
    assert after.status_code == 204
    assert accepted["status"] == "answered"
    assert second.json() == {"answer": "two"}
    assert [q for _, q in sent] == ["first?", "second?"]


async def test_a_report_during_the_long_poll_gives_409(manager, sent) -> None:
    task_id, token = await dispatch(manager)
    async with worker_client(manager, token, poll_s=5) as client:
        await client.post("/worker/ask", json={"question": "q?"})
        waiting = asyncio.create_task(client.get("/worker/answer"))
        await asyncio.sleep(0.05)
        from joshua_developer.runtime import Report

        await manager.record_report(task_id, Report(status="timed_out"))
        response = await asyncio.wait_for(waiting, 2)
    assert response.status_code == 409


async def test_the_log_goes_to_task_output_and_stops_at_its_cap(
    manager, monkeypatch, caplog
) -> None:
    from joshua_developer import store as store_module

    monkeypatch.setattr(store_module, "MAX_SESSION_LOG_BYTES", 10)
    task_id, token = await dispatch(manager)
    with caplog.at_level(logging.INFO):
        async with worker_client(manager, token) as client:
            first = await client.post("/worker/log", json={"text": "hello "})
            full = await client.post("/worker/log", json={"text": "world!"})
            again = await client.post("/worker/log", json={"text": "more"})
            small = await client.post("/worker/log", json={"text": "1"})
            bad = await client.post("/worker/log", json={"text": 3})
            huge = await client.post("/worker/log", json={"text": "x" * 300_000})
    assert first.json() == {"appended": True}
    assert full.json() == {"appended": False}
    assert again.json() == {"appended": False}
    assert small.json() == {"appended": False}
    assert bad.status_code == 400
    assert huge.status_code == 400
    row = manager.store.get_task_full(task_id)
    assert row is not None and row["session_log"] == "hello "
    drops = [r for r in caplog.records if "session log full" in str(r.msg)]
    assert len(drops) == 1
    assert "world" not in caplog.text


async def test_the_forwarder_sends_a_get_with_no_body(manager) -> None:
    upstream = Upstream()
    claude = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler))
    _, token = await dispatch(manager)
    async with worker_client(manager, token, claude_client=claude) as client:
        response = await client.get("/worker/claude/v1/models")
    await claude.aclose()
    assert response.status_code == 200
    [request] = upstream.requests
    assert str(request.url) == "https://api.anthropic.com/v1/models"
    assert request.headers["authorization"] == f"Bearer {CLAUDE}"
    assert "transfer-encoding" not in request.headers
    assert upstream.bodies == [b""]


async def test_the_forwarder_passes_the_request_on(manager) -> None:
    upstream = Upstream()
    claude = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler))
    task_id, token = await dispatch(manager)
    body = {"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi"}]}
    async with worker_client(manager, token, claude_client=claude) as client:
        response = await client.post(
            "/worker/claude/v1/messages?beta=true",
            json=body,
            headers={
                "x-api-key": "fake-key",
                "anthropic-beta": "oauth-2025-04-20",
                "anthropic-version": "2023-06-01",
                "user-agent": "claude-cli/2",
                "x-task-id": task_id,
            },
        )
    await claude.aclose()
    assert response.status_code == 200
    assert response.json() == {"id": "msg_1", "type": "message"}
    [request] = upstream.requests
    assert str(request.url) == "https://api.anthropic.com/v1/messages?beta=true"
    assert request.headers["authorization"] == f"Bearer {CLAUDE}"
    assert token not in str(request.headers)
    assert "x-api-key" not in request.headers
    assert "x-task-id" not in request.headers
    assert request.headers["host"] == "api.anthropic.com"
    assert request.headers["anthropic-beta"] == "oauth-2025-04-20"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert request.headers["user-agent"] == "claude-cli/2"
    assert json.loads(upstream.bodies[0]) == body


async def test_the_forwarder_without_a_claude_token_or_an_upstream(manager, caplog) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    claude = httpx.AsyncClient(transport=httpx.MockTransport(down))
    _, token = await dispatch(manager)
    with caplog.at_level(logging.DEBUG):
        async with worker_client(manager, token, claude_client=claude) as client:
            unreachable = await client.post("/worker/claude/v1/messages", json={})
    await claude.aclose()
    assert unreachable.status_code == 502
    assert CLAUDE not in caplog.text and token not in caplog.text

    object.__setattr__(manager.settings, "claude_token", "")
    async with worker_client(manager, token) as client:
        missing = await client.post("/worker/claude/v1/messages", json={})
    assert missing.status_code == 503
    assert missing.json() == {"error": "claude_token_not_configured"}


async def test_the_app_makes_and_closes_its_own_client(manager) -> None:
    app = build_worker_app(manager)
    _, token = await dispatch(manager)
    object.__setattr__(manager.settings, "claude_token", "")
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://d") as client:
            response = await client.get(
                "/worker/claude/v1/models", headers={"Authorization": f"Bearer {token}"}
            )
    assert response.status_code == 503


@contextlib.asynccontextmanager
async def serve(app):
    """Serve ``app`` on a real localhost socket, so a stream is not buffered."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, lifespan="off"))
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:  # noqa: ASYNC110 - uvicorn has no started event
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


async def test_the_forwarder_streams_server_sent_events(manager) -> None:
    gate = asyncio.Event()

    async def events():
        yield b"event: message_start\ndata: {}\n\n"
        await gate.wait()
        yield b"event: message_stop\ndata: {}\n\n"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=events())

    claude = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    _, token = await dispatch(manager)
    app = build_worker_app(manager, claude_client=claude)
    async with serve(app) as base:
        async with httpx.AsyncClient(base_url=base, timeout=5) as client:
            async with client.stream(
                "POST",
                "/worker/claude/v1/messages",
                json={"stream": True},
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                assert response.headers["content-type"] == "text/event-stream"
                chunks = response.aiter_raw()
                first = await asyncio.wait_for(chunks.__anext__(), 2)
                # The first event arrived while the upstream still holds the second.
                assert first == b"event: message_start\ndata: {}\n\n"
                assert not gate.is_set()
                gate.set()
                rest = b"".join([chunk async for chunk in chunks])
    await claude.aclose()
    assert rest == b"event: message_stop\ndata: {}\n\n"


async def test_a_fake_worker_drives_one_task_through_the_api(manager, sent) -> None:
    """The flow of P3.4, offline: brief, running, log, ask, answer, report."""
    task_id, token = await dispatch(manager, branch="feature")
    async with worker_client(manager, token, poll_s=2) as worker:
        brief = (await worker.get("/worker/brief")).json()
        assert brief["branch"] == "feature"
        await worker.post("/worker/running")
        await worker.post("/worker/log", json={"text": "cloned\n"})
        await worker.post("/worker/ask", json={"question": "Which port?"})
        poll = asyncio.create_task(worker.get("/worker/answer"))
        await asyncio.sleep(0.05)
        manager.answer("alex", {"task_id": task_id}, "8080")
        answer = (await poll).json()["answer"]
        await worker.post("/worker/log", json={"text": f"answer {answer}\n"})
        done = await worker.post(
            "/worker/report",
            json={"status": "success", "summary": "Port 8080.", "branch": "feature"},
        )
        after = await worker.get("/worker/brief")
    assert done.json() == {"recorded": True}
    assert after.status_code == 409
    task = manager.store.get_task_full(task_id)
    assert task is not None
    assert task["status"] == "success"
    assert task["session_log"] == "cloned\nanswer 8080\n"
    assert manager.locks.get(REPO, "branch:feature") is None
