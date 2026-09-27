"""The worker API: the routes a worker calls on the manager, on port 8001.

Every route but ``GET /healthz`` needs ``Authorization: Bearer <task token>``.
The manager mints one task token for each task when it dispatches the task.
The token names the task, so a worker reaches only its own task. A missing
or wrong token gets 401. A worker may also send ``X-Task-Id``; when it names
another task than the token, the reply is 404. A task that ended gets 409 on
every route, so the token is of no use after the report.

Routes:

- ``GET /worker/brief``: what the worker needs to do the task.
- ``POST /worker/running``: the worker started.
- ``POST /worker/report``: the report (``runtime.Report``). The task ends.
- ``POST /worker/ask`` ``{"question"}``: send a question to the person's chat.
- ``GET /worker/answer``: wait up to 25 s for the answer. 200 with the answer,
  or 204 when no answer came in that time.
- ``POST /worker/log`` ``{"text"}``: add text to the session log.
- ``/worker/claude/{path}``, any method: the Claude forwarder.

The Claude forwarder sends the request on to ``https://api.anthropic.com/{path}``.
It streams the body in both directions, so a server-sent event stream arrives
at the worker as the API sends it. It replaces ``Authorization`` with the
manager's ``CLAUDE_CODE_OAUTH_TOKEN``, drops ``x-api-key``, sets ``Host`` for
the API, and passes the other headers on. The worker sets:

    ANTHROPIC_BASE_URL=http://<MANAGER_HOST>:8001/worker/claude
    CLAUDE_CODE_OAUTH_TOKEN=<TASK_TOKEN>

The Claude CLI then sends its task token as a Claude token. It is a fake
Claude token: the real token stays in the manager.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import httpx
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from joshua_developer.log import get_logger
from joshua_developer.manager import MAX_QUESTION, Manager
from joshua_developer.runtime import Report
from joshua_developer.store import TERMINAL_STATUSES

logger = get_logger("joshua_developer.worker_api")

CLAUDE_API = "https://api.anthropic.com"
ANSWER_POLL_S = 25.0
MAX_LOG_CALL = 256 * 1024
_BEARER = "Bearer "

# Headers that never go on to the Claude API. Authorization comes back with
# the manager's token. Host comes from the upstream URL.
_DROP_REQUEST = frozenset(
    {
        "authorization",
        "x-api-key",
        "x-task-id",
        "host",
        "connection",
        "keep-alive",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_DROP_RESPONSE = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


def _error(status: int, reason: str) -> JSONResponse:
    return JSONResponse({"error": reason}, status_code=status)


def repo_url(repo: str) -> str:
    """The https clone URL of a ``host/owner/name`` repository."""
    return f"https://{repo}.git"


def build_brief(manager: Manager, task: dict[str, Any]) -> dict[str, Any]:
    """The brief of a task (the full row) for its worker."""
    persona_name = task["persona"]
    persona = manager.config.personas.get(persona_name)
    if persona is None:
        persona_name = manager.config.default_persona
        persona = manager.config.personas[persona_name]
    person = manager.effective_settings(task["person"])
    previous = task.get("report") or {}
    return {
        "task_id": task["task_id"],
        "task_type": task["task_type"],
        "repo": task["repo"],
        "repo_url": repo_url(task["repo"]),
        "branch": task["branch_name"],
        "base_branch": task["base_branch"],
        "persona": {"name": persona_name, **persona.model_dump()},
        "git": {"name": person["git_name"], "email": person["git_email"]},
        # TODO(P3.3): the git token. P3.3 decides how the worker gets it.
        "git_token": None,
        "brief": task["brief"],
        "feedback": task["instructions"],
        "answer": task["answer"],
        # Where an earlier worker stopped, for a resumed task.
        "note": previous.get("summary") or None,
    }


def build_worker_app(
    manager: Manager,
    *,
    claude_client: httpx.AsyncClient | None = None,
    claude_base: str = CLAUDE_API,
    poll_s: float = ANSWER_POLL_S,
) -> Starlette:
    """Build the worker API over ``manager``.

    ``claude_client`` is the HTTP client of the forwarder. Without one, the
    app makes one on first use and closes it at shutdown.
    """
    holder: dict[str, httpx.AsyncClient] = {}
    if claude_client is not None:
        holder["client"] = claude_client

    def client() -> httpx.AsyncClient:
        if "client" not in holder:
            holder["client"] = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)
            )
        return holder["client"]

    def authorize(request: Request) -> tuple[str, dict[str, Any]] | Response:
        raw = request.headers.get("authorization", "")
        token = raw[len(_BEARER) :] if raw.startswith(_BEARER) else ""
        task_id = manager.store.find_task_by_worker_token(token)
        if task_id is None:
            logger.warning(
                {
                    "message": "worker request refused: missing or wrong token",
                    "path": request.url.path,
                }
            )
            return _error(401, "unauthorized")
        named = request.headers.get("x-task-id")
        if named is not None and named != task_id:
            logger.warning({"message": "worker token used for another task", "task_id": task_id})
            return _error(404, "not_found")
        task = manager.store.get_task_full(task_id)
        assert task is not None
        if task["status"] in TERMINAL_STATUSES:
            return _error(409, "task_ended")
        return task_id, task

    async def json_body(request: Request) -> dict[str, Any] | None:
        try:
            data = await request.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    async def brief(request: Request) -> Response:
        found = authorize(request)
        if isinstance(found, Response):
            return found
        _, task = found
        return JSONResponse(build_brief(manager, task))

    async def running(request: Request) -> Response:
        found = authorize(request)
        if isinstance(found, Response):
            return found
        task_id, _ = found
        manager.mark_running(task_id)
        return JSONResponse({"status": "running"})

    async def report(request: Request) -> Response:
        found = authorize(request)
        if isinstance(found, Response):
            return found
        task_id, _ = found
        data = await json_body(request)
        if data is None:
            return _error(400, "bad_request")
        try:
            parsed = Report.model_validate(data)
        except ValidationError as exc:
            return JSONResponse(
                {"error": "bad_request", "detail": [e["loc"] for e in exc.errors()]},
                status_code=400,
            )
        await manager.record_report(task_id, parsed)
        return JSONResponse({"recorded": True})

    async def ask(request: Request) -> Response:
        found = authorize(request)
        if isinstance(found, Response):
            return found
        task_id, _ = found
        data = await json_body(request)
        question = data.get("question") if data else None
        if not isinstance(question, str) or not question.strip():
            return _error(400, "bad_request")
        if len(question) > MAX_QUESTION:
            return _error(400, "question_too_long")
        sent = await manager.ask(task_id, question.strip())
        return JSONResponse({"asked": True, "sent": sent}, status_code=202)

    async def answer(request: Request) -> Response:
        found = authorize(request)
        if isinstance(found, Response):
            return found
        task_id, task = found
        if not task["answer"]:
            event = manager.answer_event(task_id)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), poll_s)
            task = manager.store.get_task_full(task_id) or task
            if task["status"] in TERMINAL_STATUSES:
                return _error(409, "task_ended")
        if task["answer"]:
            return JSONResponse({"answer": task["answer"]})
        return Response(status_code=204)

    async def log(request: Request) -> Response:
        found = authorize(request)
        if isinstance(found, Response):
            return found
        task_id, _ = found
        data = await json_body(request)
        text = data.get("text") if data else None
        if not isinstance(text, str):
            return _error(400, "bad_request")
        if len(text) > MAX_LOG_CALL:
            return _error(400, "text_too_long")
        return JSONResponse({"appended": manager.append_log(task_id, text)})

    async def claude(request: Request) -> Response:
        found = authorize(request)
        if isinstance(found, Response):
            return found
        task_id, _ = found
        token = manager.settings.claude_token
        if not token:
            return _error(503, "claude_token_not_configured")
        path = request.path_params["path"]
        url = httpx.URL(f"{claude_base.rstrip('/')}/{path}")
        if request.url.query:
            url = url.copy_with(query=request.url.query.encode())
        headers = [
            (key, value)
            for key, value in request.headers.items()
            if key.lower() not in _DROP_REQUEST
        ]
        headers.append(("authorization", f"Bearer {token}"))
        has_body = "content-length" in request.headers or "transfer-encoding" in request.headers
        upstream_request = client().build_request(
            request.method,
            url,
            headers=headers,
            content=request.stream() if has_body else None,
        )
        try:
            upstream = await client().send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            logger.warning(
                {
                    "message": "the Claude API did not answer",
                    "task_id": task_id,
                    "error_type": type(exc).__name__,
                }
            )
            return _error(502, "claude_unreachable")
        response_headers = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() not in _DROP_RESPONSE
        }
        return StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=response_headers,
            background=BackgroundTask(upstream.aclose),
        )

    async def healthz(request: Request) -> Response:
        return JSONResponse({"ok": True})

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        yield
        made = holder.get("client")
        if made is not None and claude_client is None:
            await made.aclose()

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/worker/brief", brief, methods=["GET"]),
            Route("/worker/running", running, methods=["POST"]),
            Route("/worker/report", report, methods=["POST"]),
            Route("/worker/ask", ask, methods=["POST"]),
            Route("/worker/answer", answer, methods=["GET"]),
            Route("/worker/log", log, methods=["POST"]),
            Route("/worker/claude/{path:path}", claude, methods=methods),
        ],
        lifespan=lifespan,
    )
