"""The developer addon manager: coding tasks over MCP.

The addon serves MCP at ``/mcp`` over streamable HTTP. ``GET /healthz`` stays
open and names no person. When a bearer is configured (``DEVELOPER_TOKENS``
or ``ADDON_TOKEN``), every other route needs ``Authorization: Bearer
<token>``; a missing or wrong token gets 401. A ``DEVELOPER_TOKENS`` bearer
names a person. The ``ADDON_TOKEN`` bearer names no person: it may read every
task and list the personas, and every other tool gives it "403 forbidden".
A person sees only their own tasks.

``start`` also builds the worker side: the worker API for port 8001
(``worker_api``) and the git host tunnel for port 8002 (``git_proxy``).
``python -m joshua_developer`` serves all three in one process.
"""

from __future__ import annotations

import contextlib
import hmac
from collections.abc import AsyncIterator
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from joshua_developer.config import Caller, Settings, settings_from_env
from joshua_developer.git_proxy import GitProxy, task_target
from joshua_developer.locks import LockManager
from joshua_developer.log import get_logger
from joshua_developer.manager import Manager, rejected
from joshua_developer.runtime import Runtime, StubRuntime
from joshua_developer.store import TaskStore
from joshua_developer.worker_api import build_worker_app

logger = get_logger("joshua_developer")

OPEN_PATHS = frozenset({"/healthz"})
_BEARER = "Bearer "
MAX_LIST = 100

mcp = MCPServer(name="developer")

_manager: Manager | None = None
# The worker API app and the git host tunnel that start() built.
worker_app: Starlette | None = None
git_proxy: GitProxy | None = None


def _get_manager() -> Manager:
    if _manager is None:
        raise RuntimeError("build_app has not run")
    return _manager


# --- callers -------------------------------------------------------------


def find_caller(settings: Settings, authorization: str | None) -> Caller | None:
    """The caller that ``authorization`` names, or None for a missing or wrong token.

    Every configured token is compared with ``hmac.compare_digest``, so the
    time taken does not tell which token a bearer is close to. With no token
    configured, the caller has no person.
    """
    if settings.open:
        return Caller(person=None)
    raw = authorization or ""
    provided = raw[len(_BEARER) :] if raw.startswith(_BEARER) else ""
    found = None
    for token, caller in settings.tokens.items():
        if hmac.compare_digest(provided.encode(), token.encode()) and provided:
            found = caller
    return found


def _caller(ctx: Context) -> Caller:
    # The auth middleware checked this same header on the HTTP request that
    # carries this message, so here it only selects the caller.
    headers = ctx.headers or {}
    caller = find_caller(_get_manager().settings, headers.get("authorization"))
    if caller is None:
        raise ToolError("401 unauthorized")
    return caller


def _person(ctx: Context) -> str:
    caller = _caller(ctx)
    if caller.person is None:
        logger.warning({"message": "refused a person tool to a caller with no person"})
        raise ToolError("403 forbidden: this tool needs a person")
    return caller.person


def _own_task(caller: Caller, task_id: str) -> dict[str, Any]:
    """The task, when the caller may see it. Another person's task is "not found"."""
    task = _get_manager().store.get_task(task_id)
    if task is None or (caller.person is not None and task["person"] != caller.person):
        raise ToolError(f"404 not found: no task {task_id}")
    return task


# --- tools ---------------------------------------------------------------


@mcp.tool()
async def develop(
    repo: str,
    brief: str,
    ctx: Context,
    base_branch: str | None = None,
    branch: str | None = None,
    persona: str | None = None,
    notify: str | None = None,
) -> dict:
    """Start a coding task on a repository. Returns task_id, persona, and status at once.

    repo is a URL or host/owner/name, such as github.com/owner/name. brief is
    all the context the worker gets: the goal, the acceptance criteria, the
    links, and what you read in the issue. Its first line is the pull request
    title. branch is the branch to work on; without it the task gets a new
    branch, joshua/<first line in kebab case>-<id>. base_branch is the branch
    to start from and the pull request target; without it, main. persona is a name from
    list_personas. notify is the chat that gets the report; without it, the
    person's setting. A call is rejected when the repository is not in the
    person's list, when its git host has no platform or no token, when too
    many tasks run, or when another task works on the same branch.
    """
    person = _person(ctx)
    return await _get_manager().develop(
        person,
        repo,
        brief,
        base_branch=base_branch,
        branch=branch,
        persona=persona,
        notify_to=notify,
    )


@mcp.tool()
async def rework(
    repo: str,
    feedback: str,
    ctx: Context,
    pr: int | str | None = None,
    branch: str | None = None,
    persona: str | None = None,
    notify: str | None = None,
) -> dict:
    """Start a task that applies review feedback to a pull request, on its source branch.

    pr is the pull request number or its URL. The git host names the source
    branch. feedback is the change the review asks for. A plain git host has
    no pull requests: give branch there, and not pr. The other arguments are
    the same as for develop. One task at a time works on one pull request.
    """
    person = _person(ctx)
    return await _get_manager().rework(
        person, repo, pr, feedback, persona=persona, notify_to=notify, branch=branch
    )


@mcp.tool()
async def answer(task_id: str, text: str, ctx: Context) -> dict:
    """Give a task the answer to its question.

    A task waits when task_status shows an open_question. The text is the
    answer, from what you know or from what the person said. When the task
    is blocked or timed_out and its worker pushed the branch, the answer
    resumes it: a new worker continues on the branch.
    """
    person = _person(ctx)
    task = _own_task(Caller(person=person), task_id)
    return await _get_manager().answer(person, task, text)


@mcp.tool()
async def task_status(task_id: str, ctx: Context) -> dict:
    """The state of one task.

    status is dispatched, running, success, failed, blocked, or timed_out.
    The result also has the branch, the pull request URL, the cost, the
    summary, the error, the open question, and the scan result: clean,
    partial, hit, or unavailable.
    """
    return _own_task(_caller(ctx), task_id)


@mcp.tool()
async def task_output(task_id: str, ctx: Context) -> dict:
    """The full report and the session log of one task, for debugging.

    history holds the reports of the earlier workers of a resumed task.
    """
    task = _own_task(_caller(ctx), task_id)
    full = _get_manager().store.get_task_full(task["task_id"]) or {}
    report = full.get("report") or {}
    return {
        "task_id": task["task_id"],
        "status": task["status"],
        "error": task["error"],
        "report": {key: value for key, value in report.items() if key != "log"} or None,
        # The report's log when the worker sent one, else the lines from /worker/log.
        "log": report.get("log") or full.get("session_log") or "",
        "history": full.get("history") or [],
    }


@mcp.tool()
async def list_tasks(ctx: Context, limit: int = 10) -> dict:
    """The recent tasks of the person, the newest first. limit is 1 to 100."""
    caller = _caller(ctx)
    limit = max(1, min(int(limit), MAX_LIST))
    tasks = _get_manager().store.list_tasks(caller.person, limit)
    return {"tasks": tasks}


@mcp.tool()
async def get_settings(ctx: Context) -> dict:
    """The person's settings: git_name, git_email, default_persona, and notify."""
    person = _person(ctx)
    return _get_manager().effective_settings(person)


@mcp.tool()
async def set_settings(
    ctx: Context,
    git_name: str | None = None,
    git_email: str | None = None,
    default_persona: str | None = None,
    notify: str | None = None,
) -> dict:
    """Change the person's settings. Returns the settings after the change.

    Give only the fields to change. An empty string removes the change, and
    the value from the addon configuration applies again. git_name and
    git_email go on the person's commits. default_persona is a name from
    list_personas. notify is the chat that gets a report.
    """
    person = _person(ctx)
    manager = _get_manager()
    for name, value in (("git_name", git_name), ("git_email", git_email), ("notify", notify)):
        if value is not None and len(value) > 320:
            return rejected("invalid_arguments", f"{name} is longer than 320 characters")
    if git_email and ("@" not in git_email or any(c.isspace() for c in git_email)):
        return rejected("invalid_arguments", "git_email is not an email address")
    if default_persona and default_persona not in manager.config.personas:
        return rejected(
            "unknown_persona",
            f"the persona {default_persona!r} is not known; list_personas names the personas",
        )
    manager.store.set_settings(
        person,
        git_name=git_name,
        git_email=git_email,
        default_persona=default_persona,
        notify=notify,
    )
    logger.info({"message": "settings changed", "person": person})
    return manager.effective_settings(person)


@mcp.tool()
async def list_personas(ctx: Context) -> dict:
    """The personas: the model, the effort, the turn limit, and the timeout of each.

    default is the persona a task gets when the call names none.
    """
    caller = _caller(ctx)
    manager = _get_manager()
    return {
        "personas": {
            name: persona.model_dump() for name, persona in manager.config.personas.items()
        },
        "default": manager.resolve_persona(caller.person, None),
    }


# --- HTTP ----------------------------------------------------------------


class BearerAuthMiddleware:
    """Require a known bearer on every path except ``OPEN_PATHS``.

    Skips the check when no bearer is configured: the network is the
    boundary in that case.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.settings.open or scope["path"] in OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        raw = headers.get(b"authorization", b"").decode("latin-1")
        if find_caller(self.settings, raw) is None:
            logger.warning({"message": "rejected request: missing or wrong token"})
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


async def healthz(request: Request) -> Response:
    return JSONResponse({"ok": True})


mcp.custom_route("/healthz", methods=["GET"])(healthz)


def make_runtime(manager: Manager, settings: Settings, stub_delay_s: float | None) -> Runtime:
    """The runtime that ``WORKER_RUNTIME`` names."""
    if settings.worker_runtime == "docker":
        from joshua_developer.runtime_docker import DockerRuntime

        runtime = DockerRuntime(manager, settings)
        runtime.remove_orphans()
        return runtime
    if settings.worker_runtime == "kubernetes":
        from joshua_developer.runtime_k8s import KubernetesRuntime

        k8s_runtime = KubernetesRuntime(manager, settings)
        k8s_runtime.remove_orphans()
        return k8s_runtime
    if settings.worker_runtime == "stub":
        return StubRuntime(manager, delay_s=stub_delay_s)
    raise RuntimeError(f"the {settings.worker_runtime} runtime is not in this release")


def start(settings: Settings, stub_delay_s: float | None = 1.0) -> Manager:
    """Open the store, build the manager and its runtime, and recover from a restart.

    It also builds the worker API app and the git host tunnel for the manager.
    """
    global _manager, worker_app, git_proxy
    if _manager is not None:
        _manager.store.close()
    store = TaskStore(settings.db_path)
    manager = Manager(settings, store, LockManager())
    _manager = manager
    manager.recover()
    manager.runtime = make_runtime(manager, settings, stub_delay_s)
    worker_app = build_worker_app(manager)
    git_proxy = GitProxy(lambda token: task_target(manager, token))
    logger.info(
        {
            "message": "developer ready",
            "people": sorted(c.person for c in settings.tokens.values() if c.person),
            "open": settings.open,
            "runtime": settings.worker_runtime,
            "network": settings.config.network,
            "recovered": len(manager.recovered),
        }
    )
    return manager


def build_app(settings: Settings | None = None, stub_delay_s: float | None = 1.0) -> ASGIApp:
    """Build the addon's ASGI app: the MCP routes plus the auth wrapper.

    The manager starts here, before the first request. The app lifespan
    sends the reports of the tasks that the restart failed.
    ``host="0.0.0.0"`` turns off the SDK's loopback-only DNS-rebinding guard:
    a caller reaches this addon by its cluster or compose hostname, never by
    ``localhost``, and the guard would otherwise reject every real request.
    """
    settings = settings or settings_from_env()
    manager = start(settings, stub_delay_s)
    inner = mcp.streamable_http_app(streamable_http_path="/mcp", host="0.0.0.0")
    session_lifespan = inner.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with session_lifespan(app):
            await manager.send_recovered_reports()
            yield

    inner.router.lifespan_context = lifespan
    return BearerAuthMiddleware(inner, settings)
