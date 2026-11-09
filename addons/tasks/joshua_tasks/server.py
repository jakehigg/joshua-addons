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
from datetime import date
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
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


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _project_dict(p: Project) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "name": p.name,
        "slug": p.slug,
        "goal": p.goal,
        "target_date": _iso(p.target_date),
        "status": p.status,
        "archived": p.archived_at is not None,
    }


def _summary_dict(s: dict[str, Any]) -> dict[str, Any]:
    """A project summary from ``service`` as the tools return it."""
    nt = s["next_task"]
    return {
        "id": str(s["id"]),
        "name": s["name"],
        "slug": s["slug"],
        "goal": s["goal"],
        "target_date": _iso(s["target_date"]),
        "status": s["status"],
        "task_count": s["task_count"],
        "done_count": s["done_count"],
        "open_count": s["open_count"],
        "percent": s["percent"],
        "health": s["health"],
        "next_task": {"id": str(nt.id), "number": nt.number, "title": nt.title} if nt else None,
    }


def _task_ref_dict(ref: dict[str, Any]) -> dict[str, Any]:
    """One end of a blocking link: enough of that task to name it."""
    project = ref["project"]
    return {
        "id": str(ref["id"]),
        "number": ref["number"],
        "title": ref["title"],
        "done": ref["done"],
        "project": project["slug"] if project else None,
    }


def _task_dict(t: Task) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "number": t.number,
        "title": t.title,
        "notes": t.notes,
        "done": t.done,
        "size": t.size,
        "due_date": _iso(t.due_date),
        "assignee": t.assignee,
        "position": t.position,
        "group_id": str(t.group_id) if t.group_id else None,
        "blocked": t.blocked,
        "blockers": [_task_ref_dict(r) for r in t.blockers],
        "blocking": [_task_ref_dict(r) for r in t.blocking],
    }


def _found_task_dict(found: dict[str, Any]) -> dict[str, Any]:
    """One task match from ``service.search``, with its project and its group."""
    task: Task = found["task"]
    group: Group | None = found["group"]
    return {
        "id": str(task.id),
        "number": task.number,
        "title": task.title,
        "done": task.done,
        "size": task.size,
        "due_date": _iso(task.due_date),
        "assignee": task.assignee,
        "project": {"name": found["project"]["name"], "slug": found["project"]["slug"]},
        "group": {"id": str(group.id), "name": group.name} if group else None,
        "blocked": task.blocked,
        "blockers": [_task_ref_dict(r) for r in task.blockers],
    }


def _group_dict(g: Group) -> dict[str, Any]:
    return {
        "id": str(g.id),
        "name": g.name,
        "position": g.position,
        "target_date": _iso(g.target_date),
    }


def _uuid(value: str, what: str) -> uuid.UUID:
    """Parse an id argument, or raise a ``ToolError`` that names it."""
    try:
        return uuid.UUID(value)
    except ValueError:
        raise ToolError(f"{what} is not a valid id: {value}") from None


def _task_ref(value: str, what: str) -> int | uuid.UUID:
    """Parse a task argument: its short number ("24"), or its UUID.

    A value that is neither is a ``ToolError``. A number that no task has is
    a lookup the service refuses, with the number in the message.
    """
    text = value.strip()
    return int(text) if text.isdigit() else _uuid(text, what)


def _date(value: str | None, what: str) -> date | None:
    """Parse a ``YYYY-MM-DD`` argument. ``None`` or an empty string is no date."""
    if value is None or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ToolError(f"{what} must be a date as YYYY-MM-DD: {value}") from None


def _date_change(value: str | None, what: str) -> date | None:
    """For an edit: not passed keeps the date, an empty string clears it."""
    return service.UNSET if value is None else _date(value, what)


def _text_change(value: str | None) -> str | None:
    """For an edit: not passed keeps the text, an empty string clears it."""
    return service.UNSET if value is None else value


@asynccontextmanager
async def _tool_session() -> AsyncIterator[AsyncSession]:
    """Open a session; a lookup or conflict error becomes a ``ToolError``."""
    async with _session_factory()() as session:
        try:
            yield session
        except (service.NotFoundError, service.ConflictError, service.InvalidError) as exc:
            raise ToolError(str(exc)) from None


@mcp.tool()
async def list_projects(include_finished: bool = False) -> list[dict[str, Any]]:
    """
    List the projects that are not archived, with progress. Call it first.

    Each project has its id, name, slug, goal (null for a plain list),
    target_date, status (active, paused, done, closed), task_count,
    done_count, open_count, percent (done over all tasks), health, and
    next_task (the first open task in roadmap order, with its number, or
    null).

    health is one of: on_track, stalled (a goal project with no task done
    in 14 days, or one whose every open task waits for a blocker), overdue
    (past target_date with open tasks), ready_to_complete (every task done:
    call complete_project), or the status for a paused, done, or closed
    project. A list without a goal or a date has health null, and so does a
    project with no tasks yet. Use the slug to name a project in other
    tools.

    A blocked task is never the next_task. A project whose whole remaining
    work is blocked has next_task null and health stalled.

    An archived project is in no result here, with include_finished true as
    well. Use list_archived_projects for those.

    Args:
        include_finished: Set true to also see done and closed projects.
    """
    async with _tool_session() as session:
        projects = await service.list_projects(session, include_finished=include_finished)
    return [_summary_dict(p) for p in projects]


@mcp.tool()
async def create_project(
    name: str, goal: str | None = None, target_date: str | None = None
) -> dict[str, Any]:
    """
    Create a project. A url-safe unique slug is derived from the name.

    A project with a goal is meant to be completed: its tasks are the steps
    to the goal, and it gets a health signal. A project without a goal is a
    plain list, for example a backlog or a household list.

    Args:
        name: Human-readable project name, e.g. "Paint the hallway".
        goal: The outcome the tasks work toward, e.g. "Hallway painted and furniture back".
        target_date: When it should be done, as YYYY-MM-DD. Optional.
    """
    async with _tool_session() as session:
        project = await service.create_project(
            session, name, goal=goal, target_date=_date(target_date, "target_date")
        )
        return _project_dict(project)


@mcp.tool()
async def set_project_goal(
    project: str, goal: str, target_date: str | None = None
) -> dict[str, Any]:
    """
    Set or change a project's goal and target date.

    Args:
        project: Project slug or id.
        goal: The outcome the tasks work toward. An empty string removes the goal.
        target_date: YYYY-MM-DD. Omit to keep the current date; pass "" to clear it.
    """
    async with _tool_session() as session:
        await service.update_project(
            session,
            project,
            goal=goal,
            target_date=_date_change(target_date, "target_date"),
        )
        return _summary_dict(await service.project_summary(session, project))


@mcp.tool()
async def complete_project(project: str) -> dict[str, Any]:
    """
    Mark a project done. Refused while any task is open: finish, move, or
    delete the open tasks first, or use set_project_status(closed) to end the
    project with open tasks.

    Args:
        project: Project slug or id.
    """
    async with _tool_session() as session:
        await service.complete_project(session, project)
        return _summary_dict(await service.project_summary(session, project))


@mcp.tool()
async def set_project_status(
    project: str, status: Literal["active", "paused", "closed"]
) -> dict[str, Any]:
    """
    Pause, close, or reactivate a project. Use complete_project for done.

    paused keeps the project out of next_steps and out of the health
    nudges. closed ends a project that still has open tasks. active brings
    a paused, closed, or done project back.

    Args:
        project: Project slug or id.
        status: active, paused, or closed.
    """
    async with _tool_session() as session:
        await service.set_project_status(session, project, status)
        return _summary_dict(await service.project_summary(session, project))


@mcp.tool()
async def next_steps(project: str | None = None, limit: int = 3) -> list[dict[str, Any]]:
    """
    What to do next: the first open tasks of every active project, in
    roadmap order (phases in order, then the backlog). Use it for "what
    should I work on", a morning brief, or a check-in on a project.

    Each entry is a project summary (see list_projects) plus tasks, the
    next open tasks, each with its number ("task 24"). A paused or finished
    project is left out unless named, and an archived project is left out
    as well.

    A task that waits for an open blocker is not a next step and is left
    out. A project whose whole remaining work waits keeps its place in the
    answer, with an empty tasks list and the health stalled: say that the
    project waits, and name the blockers from list_tasks.

    Args:
        project: Project slug or id, to ask about one project only.
        limit: How many tasks per project (default 3).
    """
    async with _tool_session() as session:
        steps = await service.next_steps(session, project, limit=limit)
    return [{**_summary_dict(s), "tasks": [_task_dict(t) for t in s["tasks"]]} for s in steps]


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
    Archive a project: hide it everywhere, and keep its data.

    A project is archived as it stands, from any status. It does not have to
    be completed or closed first, and its status does not change. Use it for
    work nobody will finish, because that work was not done.

    An archived project is in no list_projects result (with
    include_finished true as well), in no search_tasks result, and in no
    next_steps result; its tasks are hidden with it. A tool that names the
    project by its slug or its id still reads it. list_archived_projects
    shows the archived projects, and unarchive_project brings one back.

    Args:
        project: Project slug or id.
    """
    async with _tool_session() as session:
        p = await service.update_project(session, project, archived=True)
        return {"ok": True, "project": _project_dict(p)}


@mcp.tool()
async def unarchive_project(project: str) -> dict[str, Any]:
    """
    Bring an archived project back, with the status it had.

    Use it when a project was archived by mistake. The project is in the
    lists, the searches, and the next steps again. A project that is not
    archived stays as it is.

    Args:
        project: Project slug or id, from list_archived_projects.
    """
    async with _tool_session() as session:
        p = await service.update_project(session, project, archived=False)
        return {"ok": True, "project": _project_dict(p)}


@mcp.tool()
async def list_archived_projects() -> list[dict[str, Any]]:
    """
    The archived projects, oldest first, with the same fields as
    list_projects. Nothing else shows them.

    Call it to find a project a person wants back, then unarchive_project.
    """
    async with _tool_session() as session:
        projects = await service.list_archived_projects(session)
    return [_summary_dict(p) for p in projects]


@mcp.tool()
async def list_tasks(project: str, include_done: bool = True) -> dict[str, Any]:
    """
    A project's tasks in roadmap order: each group (a phase) with its tasks,
    then the backlog (tasks in no group). The order is the order to do them
    in; project.next_task is the first open one. A group carries
    done_count/task_count/percent and an optional target_date, so you can
    report progress ("2/5 done"). A project without groups has an empty
    "groups" list and everything in "backlog".

    Each task carries its number, the short name a person says ("task 24").
    Every tool that takes a task_id takes that number as well.

    Each task also carries blocked (true while it waits for an open task),
    blockers (the tasks it waits for), and blocking (the tasks that wait for
    it). Set a link with block_task.

    Args:
        project: Project slug or id.
        include_done: Set false to only see open tasks (counts still include done).
    """
    async with _tool_session() as session:
        summary = await service.project_summary(session, project)
        tasks = await service.list_tasks(session, project, include_done=include_done)
        groups = await service.list_groups(session, project)
    by_group: dict[str, list[dict[str, Any]]] = {}
    backlog: list[dict[str, Any]] = []
    for t in tasks:
        target = by_group.setdefault(str(t.group_id), []) if t.group_id else backlog
        target.append(_task_dict(t))
    return {
        "project": _summary_dict(summary),
        "groups": [
            {
                "id": str(g["id"]),
                "name": g["name"],
                "target_date": _iso(g["target_date"]),
                "task_count": g["task_count"],
                "done_count": g["done_count"],
                "percent": g["percent"],
                "tasks": by_group.get(str(g["id"]), []),
            }
            for g in groups
        ],
        "backlog": backlog,
    }


@mcp.tool()
async def create_task(
    project: str,
    title: str,
    notes: str | None = None,
    group: str | None = None,
    size: str | None = None,
    due_date: str | None = None,
    assignee: str | None = None,
) -> dict[str, Any]:
    """
    Add a task at the end of a project's backlog, or at the end of a group
    if one is given. Use reorder_tasks after, if it belongs earlier.

    The new task gets its number, the short name a person says ("task 24").
    It is in the result, and it never changes.

    Args:
        project: Project slug or id.
        title: Short task title, e.g. "Buy paint and supplies".
        notes: Optional longer markdown notes.
        group: Optional group name or id to add the task into.
        size: Optional T-shirt size: XS, S, M, L, or XL.
        due_date: Optional date as YYYY-MM-DD.
        assignee: Optional name of the person who will do it.
    """
    async with _tool_session() as session:
        t = await service.create_task(
            session,
            project,
            title,
            notes or "",
            group=group,
            size=size,
            due_date=_date(due_date, "due_date"),
            assignee=assignee,
        )
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
async def set_group_target_date(group_id: str, target_date: str) -> dict[str, Any]:
    """
    Set the target date of a group, which makes it a milestone of the
    project's roadmap.

    Args:
        group_id: The group's id (from list_tasks).
        target_date: YYYY-MM-DD, or an empty string to clear the date.
    """
    gid = _uuid(group_id, "group_id")
    async with _tool_session() as session:
        g = await service.update_group(session, gid, target_date=_date(target_date, "target_date"))
        return _group_dict(g)


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
        task_id: The task's number ("24") or its id. Both name the same task.
        group: Group name or id; omit to make the task ungrouped.
    """
    tid = _task_ref(task_id, "task_id")
    async with _tool_session() as session:
        return _task_dict(await service.set_task_group(session, tid, group))


@mcp.tool()
async def update_task(
    task_id: str,
    title: str | None = None,
    notes: str | None = None,
    size: str | None = None,
    due_date: str | None = None,
    assignee: str | None = None,
) -> dict[str, Any]:
    """
    Edit a task. Only the arguments you pass change; an empty string clears
    size, due_date, or assignee. Use set_task_done to complete a task. An
    edit never changes the task's number.

    Args:
        task_id: The task's number ("24") or its id. Both name the same task.
        title: New title, if changing.
        notes: New notes, if changing (replaces the old notes).
        size: T-shirt size: XS, S, M, L, or XL. "" removes it.
        due_date: YYYY-MM-DD. "" removes it.
        assignee: The person's name. "" removes it.
    """
    tid = _task_ref(task_id, "task_id")
    async with _tool_session() as session:
        task = await service.update_task(
            session,
            tid,
            title=title,
            notes=notes,
            size=_text_change(size),
            due_date=_date_change(due_date, "due_date"),
            assignee=_text_change(assignee),
        )
        return _task_dict(task)


@mcp.tool()
async def set_task_done(task_id: str, done: bool) -> dict[str, Any]:
    """
    Mark a task done, or un-done (done=false) to undo a mistake.

    The result also holds "unblocked", the tasks that are free to start now
    because this task was the last blocker they waited for, and "warning",
    which is set when the task went done while a blocker of its own is still
    open. The task is never refused: tell the person about the warning, and
    ask whether the blocker is in fact done too.

    Args:
        task_id: The task's number ("24") or its id. Both name the same task.
        done: true to complete, false to reopen.
    """
    tid = _task_ref(task_id, "task_id")
    async with _tool_session() as session:
        report = await service.set_task_done(session, tid, done)
    return {
        **_task_dict(report["task"]),
        "unblocked": [_task_ref_dict(r) for r in report["unblocked"]],
        "warning": report["warning"],
    }


@mcp.tool()
async def block_task(task_id: str, blocker_id: str) -> dict[str, Any]:
    """
    Make a task wait for another task: task_id cannot start before
    blocker_id is done. Use it when a person says "X is blocked on Y".

    While a blocker is open, the task is blocked: it never comes back from
    next_steps and it is never a project's next_task. The task unblocks
    itself as soon as the last open blocker is done — there is nothing to
    clear by hand. The blocker can be in another project.

    A task cannot block itself, and a link that makes a loop is refused with
    an error that names the tasks in the loop.

    Args:
        task_id: The number ("27") or the id of the task that waits. Both name the same task.
        blocker_id: The number ("24") or the id of the task it waits for.
    """
    tid = _task_ref(task_id, "task_id")
    bid = _task_ref(blocker_id, "blocker_id")
    async with _tool_session() as session:
        return _task_dict(await service.block_task(session, tid, bid))


@mcp.tool()
async def unblock_task(task_id: str, blocker_id: str) -> dict[str, Any]:
    """
    Remove one blocking link, because the two tasks are not in fact related.
    A done blocker needs no removal: set_task_done unblocks the task.

    A link that is not there is no error.

    Args:
        task_id: The number ("27") or the id of the task that waits. Both name the same task.
        blocker_id: The number ("24") or the id of the task it waited for.
    """
    tid = _task_ref(task_id, "task_id")
    bid = _task_ref(blocker_id, "blocker_id")
    async with _tool_session() as session:
        return _task_dict(await service.unblock_task(session, tid, bid))


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
        ordered_task_ids: Every task of that list, by number ("24") or by id, in the new order.
        group: Group name or id; omit for the ungrouped list.
    """
    ids = [_task_ref(i, "ordered_task_ids entry") for i in ordered_task_ids]
    async with _tool_session() as session:
        tasks = await service.reorder_tasks(session, project, ids, group=group)
    return {"ok": True, "order": [str(t.id) for t in tasks]}


@mcp.tool()
async def search_tasks(
    query: str,
    include_done: bool = False,
    project: str | None = None,
    limit: int = service.SEARCH_LIMIT,
) -> dict[str, Any]:
    """
    Search the projects and the tasks for a word. Call it before
    create_task, to see whether the task is there already.

    query is matched as a substring, and case does not matter: "paint"
    finds "Buy Paint". A project matches on its name or its goal, a task on
    its title or its notes.

    The result keeps the two kinds apart: "projects", the project summaries
    (see list_projects), and "tasks", the matching tasks. Each task carries
    its id, its number ("task 24"), title, done, size, due_date, assignee,
    its project (name and slug), its group (name and id, or null when the
    task is in the backlog), blocked, and blockers (the tasks it waits
    for). The projects come oldest first. The tasks come project by
    project, in that same project order, and in roadmap order inside a
    project (the phases in order, then the backlog).

    An archived project, and every task in it, is never searched. An empty
    query matches nothing.

    Args:
        query: The word or phrase to look for.
        include_done: Set true to also match done tasks and done or closed projects.
        project: Project slug or id, to match tasks in one project only.
        limit: How many projects, and how many tasks, at most (default 20).
    """
    async with _tool_session() as session:
        found = await service.search(
            session, query, include_done=include_done, project=project, limit=limit
        )
    return {
        "projects": [_summary_dict(p) for p in found["projects"]],
        "tasks": [_found_task_dict(f) for f in found["tasks"]],
    }


@mcp.tool()
async def move_task(task_id: str, to_project: str) -> dict[str, Any]:
    """
    Move a task to another project (appended at the end of that project).
    The task keeps its number.

    Args:
        task_id: The task's number ("24") or its id. Both name the same task.
        to_project: Destination project slug or id.
    """
    tid = _task_ref(task_id, "task_id")
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
    from joshua_tasks.api import build_api_app
    from joshua_tasks.static import build_static_app

    inner = mcp.streamable_http_app(streamable_http_path="/mcp", host="0.0.0.0")
    inner.mount("/api", build_api_app())
    static_app = build_static_app()
    if static_app is not None:
        inner.mount("/", static_app)

    token = os.environ.get("ADDON_TOKEN") or None
    return BearerAuthMiddleware(inner, token)
