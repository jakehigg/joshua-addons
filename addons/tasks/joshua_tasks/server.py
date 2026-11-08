"""The tasks addon: MCP tools for projects, groups, and tasks.

The addon is a copy of the pantry addon's shape (``docs/adding-an-addon.md``):
it serves MCP at ``/mcp``, the streamable-HTTP transport the joshua-ai gateway
expects from a ``type: http`` upstream. ``GET /healthz`` stays open. Every
other MCP route needs ``Authorization: Bearer <ADDON_TOKEN>`` when
``ADDON_TOKEN`` is set; with no ``ADDON_TOKEN``, the docker network is the
boundary.
"""

from __future__ import annotations

import hmac
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from joshua_tasks import service
from joshua_tasks.database import build_engine, build_sessionmaker
from joshua_tasks.log import get_logger
from joshua_tasks.migrations import run_migrations
from joshua_tasks.models import Group, Project, Task

logger = get_logger("tasks")

# Only ``/mcp`` needs a bearer token. ``/healthz``, ``/api/*`` (the REST API
# behind the web UI), and the UI itself at ``/`` stay open.
PROTECTED_PREFIX = "/mcp"

# Set by ``lifespan`` on startup, cleared on shutdown. A tool called before
# startup (or after shutdown) fails loudly instead of touching no database.
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _session_factory() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("the database is not ready; the server lifespan has not started")
    return _sessionmaker


@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[dict[str, Any]]:
    """Build the engine, run migrations, and open the session factory.

    Runs once, when the streamable-HTTP session manager starts (see
    ``build_app``). ``DATABASE_URL``/``TASKS_DB`` are read here, not at
    import time, so a caller (a test, for example) can set them first.
    """
    global _sessionmaker
    engine = build_engine()
    await run_migrations(engine)
    _sessionmaker = build_sessionmaker(engine)
    try:
        yield {}
    finally:
        _sessionmaker = None
        await engine.dispose()


mcp = MCPServer(name="tasks", lifespan=lifespan)


def _project_dict(p: Project) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "name": p.name,
        "slug": p.slug,
        "archived": p.archived_at is not None,
    }


def _task_dict(t: Task) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "title": t.title,
        "notes": t.notes,
        "done": t.done,
        "position": t.position,
        "group_id": str(t.group_id) if t.group_id else None,
    }


def _group_dict(g: Group) -> dict[str, Any]:
    return {"id": str(g.id), "name": g.name, "position": g.position}


def _uuid(value: str, what: str) -> uuid.UUID:
    """Parse an id argument, or raise a ``ToolError`` that names it."""
    try:
        return uuid.UUID(value)
    except ValueError:
        raise ToolError(f"{what} is not a valid id: {value}") from None


@asynccontextmanager
async def _tool_session() -> AsyncIterator[AsyncSession]:
    """Open a session; a lookup or conflict error becomes a ``ToolError``."""
    async with _session_factory()() as session:
        try:
            yield session
        except (service.NotFoundError, service.ConflictError) as exc:
            raise ToolError(str(exc)) from None


@mcp.tool()
async def list_projects() -> list[dict[str, Any]]:
    """
    List all active (non-archived) projects with task counts.

    Returns each project's id, name, slug, task_count (all tasks) and
    done_count (completed tasks). Use the slug to reference a project in
    the other tools.
    """
    async with _tool_session() as session:
        projects = await service.list_projects(session)
    return [
        {
            "id": str(p["id"]),
            "name": p["name"],
            "slug": p["slug"],
            "task_count": p["task_count"],
            "done_count": p["done_count"],
        }
        for p in projects
    ]


@mcp.tool()
async def create_project(name: str) -> dict[str, Any]:
    """
    Create a new project. A url-safe unique slug is derived from the name.

    Args:
        name: Human-readable project name, e.g. "Paint the hallway".
    """
    async with _tool_session() as session:
        return _project_dict(await service.create_project(session, name))


@mcp.tool()
async def rename_project(project: str, name: str) -> dict[str, Any]:
    """
    Rename a project. The slug stays stable — only the display name changes.

    Args:
        project: Project slug or id.
        name: The new name.
    """
    async with _tool_session() as session:
        return _project_dict(await service.update_project(session, project, name=name))


@mcp.tool()
async def archive_project(project: str) -> dict[str, Any]:
    """
    Archive a project — hides it from the project list but keeps its data.
    (There is no unarchive tool yet; ask a human to restore via the API.)

    Args:
        project: Project slug or id.
    """
    async with _tool_session() as session:
        p = await service.update_project(session, project, archived=True)
        return {"ok": True, "project": _project_dict(p)}


@mcp.tool()
async def list_tasks(project: str, include_done: bool = True) -> dict[str, Any]:
    """
    List a project's tasks in display order: the ungrouped list first, then
    each group with its tasks. Groups carry done_count/task_count so you can
    report progress ("2/5 done"). Projects without groups just have an empty
    "groups" list — everything else works the same.

    Args:
        project: Project slug or id.
        include_done: Set false to only see open tasks (counts still include done).
    """
    async with _tool_session() as session:
        tasks = await service.list_tasks(session, project, include_done=include_done)
        groups = await service.list_groups(session, project)
    by_group: dict[str, list[dict[str, Any]]] = {}
    ungrouped: list[dict[str, Any]] = []
    for t in tasks:
        target = by_group.setdefault(str(t.group_id), []) if t.group_id else ungrouped
        target.append(_task_dict(t))
    return {
        "ungrouped": ungrouped,
        "groups": [
            {
                "id": str(g["id"]),
                "name": g["name"],
                "task_count": g["task_count"],
                "done_count": g["done_count"],
                "tasks": by_group.get(str(g["id"]), []),
            }
            for g in groups
        ],
    }


@mcp.tool()
async def create_task(
    project: str, title: str, notes: str | None = None, group: str | None = None
) -> dict[str, Any]:
    """
    Add a task at the end of a project's list — or at the end of a group,
    if one is given.

    Args:
        project: Project slug or id.
        title: Short task title, e.g. "Buy paint and supplies".
        notes: Optional longer markdown notes.
        group: Optional group name or id to add the task into.
    """
    async with _tool_session() as session:
        t = await service.create_task(session, project, title, notes or "", group=group)
        return _task_dict(t)


@mcp.tool()
async def create_group(project: str, name: str) -> dict[str, Any]:
    """
    Create a task group in a project (single level — groups can't nest).
    New groups appear after the ungrouped task list; add tasks with
    create_task(group=...) or move existing ones with set_task_group.

    Args:
        project: Project slug or id.
        name: Group name, e.g. "Prep work".
    """
    async with _tool_session() as session:
        return _group_dict(await service.create_group(session, project, name))


@mcp.tool()
async def rename_group(group_id: str, name: str) -> dict[str, Any]:
    """
    Rename a task group.

    Args:
        group_id: The group's id (from list_tasks).
        name: The new name.
    """
    gid = _uuid(group_id, "group_id")
    async with _tool_session() as session:
        return _group_dict(await service.rename_group(session, gid, name))


@mcp.tool()
async def delete_group(group_id: str) -> dict[str, Any]:
    """
    Delete a task group. Its tasks are NOT deleted — they move to the end
    of the project's ungrouped list.

    Args:
        group_id: The group's id (from list_tasks).
    """
    gid = _uuid(group_id, "group_id")
    async with _tool_session() as session:
        await service.delete_group(session, gid)
    return {"ok": True}


@mcp.tool()
async def set_task_group(task_id: str, group: str | None = None) -> dict[str, Any]:
    """
    Move a task into a group, or out of its group (omit `group` to ungroup).
    The task lands at the end of the target list.

    Args:
        task_id: The task's id (from list_tasks).
        group: Group name or id; omit to make the task ungrouped.
    """
    tid = _uuid(task_id, "task_id")
    async with _tool_session() as session:
        return _task_dict(await service.set_task_group(session, tid, group))


@mcp.tool()
async def update_task(
    task_id: str, title: str | None = None, notes: str | None = None
) -> dict[str, Any]:
    """
    Edit a task's title and/or notes. Use set_task_done to complete a task.

    Args:
        task_id: The task's id (from list_tasks).
        title: New title, if changing.
        notes: New notes, if changing (replaces the old notes).
    """
    tid = _uuid(task_id, "task_id")
    async with _tool_session() as session:
        return _task_dict(await service.update_task(session, tid, title=title, notes=notes))


@mcp.tool()
async def set_task_done(task_id: str, done: bool) -> dict[str, Any]:
    """
    Mark a task done, or un-done (done=false) to undo a mistake.

    Args:
        task_id: The task's id (from list_tasks).
        done: true to complete, false to reopen.
    """
    tid = _uuid(task_id, "task_id")
    async with _tool_session() as session:
        return _task_dict(await service.update_task(session, tid, done=done))


@mcp.tool()
async def reorder_tasks(
    project: str, ordered_task_ids: list[str], group: str | None = None
) -> dict[str, Any]:
    """
    Set the task order of ONE list: the project's ungrouped list (default)
    or one group (pass `group`). ordered_task_ids becomes that list's full
    content, top to bottom — include every task currently in it (done ones
    too; fetch with list_tasks first). Listing a task that's currently in a
    different list moves it into this one.

    Args:
        project: Project slug or id.
        ordered_task_ids: Every task id for that list, in the new order.
        group: Group name or id; omit for the ungrouped list.
    """
    ids = [_uuid(i, "ordered_task_ids entry") for i in ordered_task_ids]
    async with _tool_session() as session:
        tasks = await service.reorder_tasks(session, project, ids, group=group)
    return {"ok": True, "order": [str(t.id) for t in tasks]}


@mcp.tool()
async def move_task(task_id: str, to_project: str) -> dict[str, Any]:
    """
    Move a task to another project (appended at the end of that project).

    Args:
        task_id: The task's id.
        to_project: Destination project slug or id.
    """
    tid = _uuid(task_id, "task_id")
    async with _tool_session() as session:
        return _task_dict(await service.move_task(session, tid, to_project))


class BearerAuthMiddleware:
    """Require ``Authorization: Bearer <token>`` on ``PROTECTED_PREFIX`` only.

    ``/healthz``, ``/api/*``, and the served UI at ``/`` stay open. Skips the
    check entirely when ``token`` is ``None`` (no ``ADDON_TOKEN``
    configured): the docker network is the boundary in that case.
    """

    def __init__(self, app: ASGIApp, token: str | None) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        protected = path == PROTECTED_PREFIX or path.startswith(PROTECTED_PREFIX + "/")
        if scope["type"] != "http" or self.token is None or not protected:
            await self.app(scope, receive, send)
            return

        headers = dict(scope["headers"])
        raw = headers.get(b"authorization", b"").decode("latin-1")
        prefix = "Bearer "
        provided = raw[len(prefix) :] if raw.startswith(prefix) else ""
        if not provided or not hmac.compare_digest(provided, self.token):
            logger.warning({"message": "rejected request: missing or wrong token"})
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


async def healthz(request: Request) -> Response:
    return JSONResponse({"ok": True})


mcp.custom_route("/healthz", methods=["GET"])(healthz)


def build_app() -> ASGIApp:
    """Build the addon's ASGI app: MCP, the REST API mount, the built UI, and the auth wrapper.

    ``host="0.0.0.0"`` turns off the SDK's loopback-only DNS-rebinding guard: a
    caller reaches this addon by its compose or cluster hostname (for example
    ``http://tasks:8000/mcp``), never by ``localhost``, and the guard would
    otherwise reject every real request.

    ``/api`` and the built frontend are mounted onto the same ``Starlette``
    instance the MCP SDK returns, after it registered ``/mcp`` and
    ``/healthz``. Starlette tries routes in registration order, so those two
    match first, then ``/api``, then the catch-all UI mount at ``/`` last.
    """
    from joshua_tasks.static import build_static_app

    inner = mcp.streamable_http_app(streamable_http_path="/mcp", host="0.0.0.0")
    # The REST API (P5.4) goes here. Empty until then: every /api path is a 404.
    inner.mount("/api", Starlette())
    static_app = build_static_app()
    if static_app is not None:
        inner.mount("/", static_app)

    token = os.environ.get("ADDON_TOKEN") or None
    return BearerAuthMiddleware(inner, token)
