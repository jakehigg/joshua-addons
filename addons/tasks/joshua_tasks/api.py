"""The REST API behind the web UI, and the live-update stream.

Mounted at ``/api`` on the same Starlette app that serves ``/mcp`` and
``/healthz`` (see ``server.build_app``). Every route is open: a deployer
gates access with the ingress or the docker network, as with the v3 app.

Paths, methods, status codes, and JSON shapes are the v3 FastAPI app's, so
the web UI's ``api.ts`` works unchanged. Validation errors answer ``422`` with
a FastAPI-shaped ``{"detail": [{"type", "loc", "msg", ...}]}`` body; every
other error is ``{"detail": "<message>"}``.

Each handler opens its own session and calls ``joshua_tasks.service``, the
same functions the MCP tools call, so the UI and Joshua cannot disagree.
``joshua_tasks.server`` is imported for its module-level session factory only.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

import joshua_tasks.server as server
from joshua_tasks import service
from joshua_tasks.events import broadcaster, current_origin
from joshua_tasks.models import PROJECT_STATUSES, TASK_SIZES

ProjectStatus = Literal[PROJECT_STATUSES]  # type: ignore[valid-type]
TaskSize = Literal[TASK_SIZES]  # type: ignore[valid-type]

# nginx-ingress cuts an idle stream at 60 s; a comment line every 25 s keeps it open.
KEEPALIVE_SECONDS = 25.0

_UUID = TypeAdapter(uuid.UUID)
_BOOL = TypeAdapter(bool)
_INT = TypeAdapter(int)


# --- schemas ---------------------------------------------------------------


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    goal: str | None = Field(default=None, max_length=2000)
    target_date: date | None = None


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool | None = None
    # Presence-sensitive: ``goal: null`` clears the goal, omitting it leaves
    # it alone (checked through ``model_fields_set``). Same for target_date.
    goal: str | None = Field(default=None, max_length=2000)
    target_date: date | None = None
    # ``done`` is refused with a 409 while a task is open.
    status: ProjectStatus | None = None


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    # The short number a person says ("task 24"); unique over every project.
    number: int
    project_id: uuid.UUID
    group_id: uuid.UUID | None
    title: str
    notes: str
    done: bool
    done_at: datetime | None
    size: str | None = None
    due_date: date | None = None
    assignee: str | None = None
    position: float
    created_at: datetime
    updated_at: datetime


class ProjectOut(BaseModel):
    """A project summary: the stored fields plus the computed ones."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    goal: str | None = None
    target_date: date | None = None
    status: str = "active"
    completed_at: datetime | None = None
    created_at: datetime
    archived_at: datetime | None
    task_count: int = 0
    done_count: int = 0
    open_count: int = 0
    percent: int = 0
    health: str | None = None
    last_progress_at: datetime | None = None
    next_task: TaskOut | None = None


class NextStepsOut(ProjectOut):
    tasks: list[TaskOut] = []


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class GroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    # Presence-sensitive, as ``ProjectUpdate.target_date``.
    target_date: date | None = None


class GroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    position: float
    target_date: date | None = None
    created_at: datetime
    updated_at: datetime
    task_count: int = 0
    done_count: int = 0
    percent: int = 0


class GroupOrder(BaseModel):
    ordered_ids: list[uuid.UUID]


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=1000)
    notes: str = ""
    group_id: uuid.UUID | None = None
    size: TaskSize | None = None
    due_date: date | None = None
    assignee: str | None = Field(default=None, max_length=200)


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=1000)
    notes: str | None = None
    done: bool | None = None
    # Presence-sensitive: ``group_id: null`` ungroups, omitting it leaves the
    # task alone (checked through ``model_fields_set``). ``size``,
    # ``due_date``, and ``assignee`` clear on ``null`` the same way.
    group_id: uuid.UUID | None = None
    size: TaskSize | None = None
    due_date: date | None = None
    assignee: str | None = Field(default=None, max_length=200)


class TaskOrder(BaseModel):
    ordered_ids: list[uuid.UUID]
    # null/omitted = the ungrouped root list, a group id = that group.
    group_id: uuid.UUID | None = None


class SearchTaskOut(BaseModel):
    """One task that matches a search: the task, its project, and its group."""

    model_config = ConfigDict(from_attributes=True)

    task: TaskOut
    project: ProjectOut
    # null when the task is in the project's backlog.
    group: GroupOut | None = None


class SearchOut(BaseModel):
    """The two kinds of match, apart: the projects first, then the tasks."""

    model_config = ConfigDict(from_attributes=True)

    projects: list[ProjectOut] = []
    tasks: list[SearchTaskOut] = []


# --- plumbing ----------------------------------------------------------------


class _Invalid(Exception):
    """A request the schema rejects; answered as ``422``."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors = errors


def _errors(exc: ValidationError, *where: str) -> list[dict[str, Any]]:
    errors = json.loads(exc.json(include_url=False))
    for error in errors:
        error["loc"] = [*where, *error["loc"]]
    return errors


def _path_uuid(request: Request, name: str) -> uuid.UUID:
    try:
        return _UUID.validate_python(request.path_params[name])
    except ValidationError as exc:
        raise _Invalid(_errors(exc, "path", name)) from None


def _path_int(request: Request, name: str) -> int:
    try:
        return _INT.validate_python(request.path_params[name])
    except ValidationError as exc:
        raise _Invalid(_errors(exc, "path", name)) from None


def _query_bool(request: Request, name: str, default: bool) -> bool:
    """A flag from the query string (``true``/``false``, ``1``/``0``); 422 on anything else."""
    raw = request.query_params.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return _BOOL.validate_python(raw.strip())
    except ValidationError as exc:
        raise _Invalid(_errors(exc, "query", name)) from None


async def _body[M: BaseModel](request: Request, model: type[M]) -> M:
    try:
        raw = await request.json()
    except ValueError as exc:
        raise _Invalid(
            [
                {
                    "type": "json_invalid",
                    "loc": ["body"],
                    "msg": "JSON decode error",
                    "input": str(exc),
                }
            ]
        ) from None
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise _Invalid(_errors(exc, "body")) from None


def _json(model: type[BaseModel], value: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        model.model_validate(value).model_dump(mode="json"), status_code=status_code
    )


def _json_list(model: type[BaseModel], values: list[Any]) -> JSONResponse:
    return JSONResponse([model.model_validate(v).model_dump(mode="json") for v in values])


def _detail(status_code: int, detail: Any) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status_code)


Handler = Callable[[Request], Awaitable[Response]]


def _guarded(handler: Handler) -> Handler:
    """Map service and validation errors to the v3 status codes."""

    async def wrapper(request: Request) -> Response:
        try:
            return await handler(request)
        except _Invalid as exc:
            return _detail(422, exc.errors)
        except service.NotFoundError as exc:
            return _detail(404, str(exc))
        except service.ConflictError as exc:
            return _detail(409, str(exc))
        except service.InvalidError as exc:
            return _detail(422, str(exc))

    wrapper.__name__ = handler.__name__
    wrapper.__doc__ = handler.__doc__
    return wrapper


def _session():
    return server._session_factory()()


class EventOriginMiddleware:
    """Tag change events with the writing tab's ``X-Client-Id``.

    The tab ignores events whose ``origin`` is its own id. A request without
    the header (and every MCP call, which never passes through here) sets
    ``None``, so every open tab refreshes on those changes.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw = dict(scope["headers"]).get(b"x-client-id")
        token = current_origin.set(raw.decode("latin-1") if raw else None)
        try:
            await self.app(scope, receive, send)
        finally:
            current_origin.reset(token)


# --- health and events -----------------------------------------------------


async def health(request: Request) -> Response:
    """Kept for the v3 UI; ``/healthz`` is the probe."""
    return JSONResponse({"status": "ok"})


async def events(request: Request) -> Response:
    """SSE stream of change notifications: one small JSON event per mutation.

    Clients refetch on relevant events instead of polling. A keepalive comment
    every ``KEEPALIVE_SECONDS`` stops nginx-ingress from cutting the stream.
    """
    queue = broadcaster.subscribe()

    async def stream() -> AsyncIterator[str]:
        try:
            yield ": connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                    yield f"data: {json.dumps(event)}\n\n"
                except TimeoutError:
                    yield ": ping\n\n"
        finally:
            broadcaster.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # nginx buffers responses by default, which would hold events back
            "X-Accel-Buffering": "no",
        },
    )


# --- projects ----------------------------------------------------------------


async def list_projects(request: Request) -> Response:
    async with _session() as session:
        projects = await service.list_projects(session)
    return _json_list(ProjectOut, projects)


async def create_project(request: Request) -> Response:
    body = await _body(request, ProjectCreate)
    async with _session() as session:
        project = await service.create_project(
            session, body.name, goal=body.goal, target_date=body.target_date
        )
        summary = await service.project_summary(session, project.id)
    return _json(ProjectOut, summary, 201)


async def update_project(request: Request) -> Response:
    project_id = _path_uuid(request, "project_id")
    body = await _body(request, ProjectUpdate)
    given = body.model_fields_set
    async with _session() as session:
        await service.update_project(
            session,
            project_id,
            name=body.name,
            archived=body.archived,
            goal=body.goal if "goal" in given else service.UNSET,
            target_date=body.target_date if "target_date" in given else service.UNSET,
        )
        if body.status is not None:
            await service.set_project_status(session, project_id, body.status)
        summary = await service.project_summary(session, project_id)
    return _json(ProjectOut, summary)


async def next_steps(request: Request) -> Response:
    """``?project=<id|slug>`` narrows to one project; ``?limit=`` caps the tasks (default 3)."""
    project = request.query_params.get("project") or None
    try:
        limit = int(request.query_params.get("limit", "3"))
    except ValueError:
        raise _Invalid(
            [{"type": "int_parsing", "loc": ["query", "limit"], "msg": "must be an integer"}]
        ) from None
    async with _session() as session:
        steps = await service.next_steps(session, project, limit=limit)
    return _json_list(NextStepsOut, steps)


async def delete_project(request: Request) -> Response:
    project_id = _path_uuid(request, "project_id")
    async with _session() as session:
        await service.delete_project(session, project_id)
    return Response(status_code=204)


# --- groups ------------------------------------------------------------------


async def list_groups(request: Request) -> Response:
    project_id = _path_uuid(request, "project_id")
    async with _session() as session:
        groups = await service.list_groups(session, project_id)
    return _json_list(GroupOut, groups)


async def create_group(request: Request) -> Response:
    project_id = _path_uuid(request, "project_id")
    body = await _body(request, GroupCreate)
    async with _session() as session:
        group = await service.create_group(session, project_id, body.name)
    return _json(GroupOut, group, 201)


async def update_group(request: Request) -> Response:
    group_id = _path_uuid(request, "group_id")
    body = await _body(request, GroupUpdate)
    given = body.model_fields_set
    async with _session() as session:
        group = await service.update_group(
            session,
            group_id,
            name=body.name,
            target_date=body.target_date if "target_date" in given else service.UNSET,
        )
    return _json(GroupOut, group)


async def delete_group(request: Request) -> Response:
    group_id = _path_uuid(request, "group_id")
    async with _session() as session:
        await service.delete_group(session, group_id)
    return Response(status_code=204)


async def reorder_groups(request: Request) -> Response:
    project_id = _path_uuid(request, "project_id")
    body = await _body(request, GroupOrder)
    async with _session() as session:
        groups = await service.reorder_groups(session, project_id, body.ordered_ids)
    return _json_list(GroupOut, groups)


# --- tasks -------------------------------------------------------------------


async def list_tasks(request: Request) -> Response:
    project_id = _path_uuid(request, "project_id")
    async with _session() as session:
        tasks = await service.list_tasks(session, project_id)
    return _json_list(TaskOut, tasks)


async def create_task(request: Request) -> Response:
    project_id = _path_uuid(request, "project_id")
    body = await _body(request, TaskCreate)
    async with _session() as session:
        task = await service.create_task(
            session,
            project_id,
            body.title,
            body.notes,
            group=body.group_id,
            size=body.size,
            due_date=body.due_date,
            assignee=body.assignee,
        )
    return _json(TaskOut, task, 201)


async def task_by_number(request: Request) -> Response:
    """``GET /api/tasks/by-number/{number}``: one task by its short number.

    The number is the name a person says ("task 24"). A number no task has
    answers ``404``.
    """
    number = _path_int(request, "number")
    async with _session() as session:
        task = await service.resolve_task(session, number)
    return _json(TaskOut, task)


async def reorder_tasks(request: Request) -> Response:
    project_id = _path_uuid(request, "project_id")
    body = await _body(request, TaskOrder)
    async with _session() as session:
        tasks = await service.reorder_tasks(
            session, project_id, body.ordered_ids, group=body.group_id
        )
    return _json_list(TaskOut, tasks)


async def update_task(request: Request) -> Response:
    task_id = _path_uuid(request, "task_id")
    body = await _body(request, TaskUpdate)
    given = body.model_fields_set
    async with _session() as session:
        task = await service.update_task(
            session,
            task_id,
            title=body.title,
            notes=body.notes,
            done=body.done,
            size=body.size if "size" in given else service.UNSET,
            due_date=body.due_date if "due_date" in given else service.UNSET,
            assignee=body.assignee if "assignee" in given else service.UNSET,
        )
        if "group_id" in body.model_fields_set:
            task = await service.set_task_group(session, task_id, body.group_id)
    return _json(TaskOut, task)


async def delete_task(request: Request) -> Response:
    task_id = _path_uuid(request, "task_id")
    async with _session() as session:
        await service.delete_task(session, task_id)
    return Response(status_code=204)


# --- search ------------------------------------------------------------------


async def search(request: Request) -> Response:
    """``?query=`` is the text to find.

    The match is a case-insensitive substring, on a project's name or goal
    and on a task's title or notes. ``?include_done=true`` also matches
    done tasks and done or closed projects; an archived project is never
    matched. ``?project=<id|slug>`` narrows the task match to one project.
    ``?limit=`` caps each list (default 20).
    """
    query = request.query_params.get("query", "")
    project = request.query_params.get("project") or None
    include_done = _query_bool(request, "include_done", False)
    try:
        limit = int(request.query_params.get("limit", str(service.SEARCH_LIMIT)))
    except ValueError:
        raise _Invalid(
            [{"type": "int_parsing", "loc": ["query", "limit"], "msg": "must be an integer"}]
        ) from None
    async with _session() as session:
        found = await service.search(
            session, query, include_done=include_done, project=project, limit=limit
        )
    return _json(SearchOut, found)


def _routes() -> list[Route]:
    def route(path: str, handler: Handler, method: str) -> Route:
        return Route(path, _guarded(handler), methods=[method])

    return [
        route("/health", health, "GET"),
        route("/events", events, "GET"),
        route("/projects", list_projects, "GET"),
        route("/projects", create_project, "POST"),
        route("/next-steps", next_steps, "GET"),
        route("/search", search, "GET"),
        route("/projects/{project_id}", update_project, "PATCH"),
        route("/projects/{project_id}", delete_project, "DELETE"),
        route("/projects/{project_id}/groups", list_groups, "GET"),
        route("/projects/{project_id}/groups", create_group, "POST"),
        route("/projects/{project_id}/groups/order", reorder_groups, "PUT"),
        route("/groups/{group_id}", update_group, "PATCH"),
        route("/groups/{group_id}", delete_group, "DELETE"),
        route("/projects/{project_id}/tasks", list_tasks, "GET"),
        route("/projects/{project_id}/tasks", create_task, "POST"),
        route("/projects/{project_id}/tasks/order", reorder_tasks, "PUT"),
        route("/tasks/by-number/{number}", task_by_number, "GET"),
        route("/tasks/{task_id}", update_task, "PATCH"),
        route("/tasks/{task_id}", delete_task, "DELETE"),
    ]


def build_api_app() -> ASGIApp:
    """Build the ``/api`` sub-app. No auth: see the module docstring."""
    app = Starlette(routes=_routes())
    # v3 answered ``/api/projects/`` with a 404, not a redirect.
    app.router.redirect_slashes = False
    return EventOriginMiddleware(app)
