"""Service layer — the single source of truth for all task/project operations.

Both the REST API (joshua_tasks.api) and the MCP tools (joshua_tasks.server) call these
functions, so behavior can never drift between the two surfaces. Every
function takes an AsyncSession and commits its own work.

Ordering: tasks carry a float `position` (smaller = higher). Appends go to
max+STEP; a reorder is authoritative and rewrites the whole project's
positions as (index+1)*STEP, so precision never degrades.

Roadmap order: a project's groups are its phases, in their own order, each
with its tasks in order; the ungrouped list is the backlog and comes last.
The first open task in that order is the project's next step. A project with
no groups is one ordered list, as before.

Summaries: ``list_projects``, ``project_summary``, ``next_steps``, and
``search`` return dicts with computed fields (``percent``, ``health``,
``next_task``). Nothing computed is stored.

Blocking: a task can wait for other tasks (``task_blockers``, which may cross
projects). ``blocked``, ``blockers``, and ``blocking`` are computed from those
links on every read, so a task unblocks itself the moment the last open
blocker is done. A blocked task is never the next task and never reaches
``next_steps``.
"""

import re
import uuid
from collections.abc import Collection, Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .events import broadcaster
from .models import (
    FINISHED_STATUSES,
    PROJECT_STATUSES,
    TASK_SIZES,
    Group,
    Project,
    Task,
    TaskBlocker,
)

POSITION_STEP = 1024.0

# A goal project with no task done for this many days is ``stalled``.
STALLED_AFTER_DAYS: Final = 14

# How many projects, and how many tasks, a search returns by default.
SEARCH_LIMIT: Final = 20


class _Unset:
    """Marks an optional argument the caller did not pass, so ``None`` can clear a value."""

    def __repr__(self) -> str:
        return "UNSET"


UNSET: Any = _Unset()


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class InvalidError(Exception):
    """An argument value the service refuses (a bad size or status)."""


def _today() -> date:
    return datetime.now(UTC).date()


def _clean(value: str | None) -> str | None:
    """Strip a free-text field; an empty string clears it to ``None``."""
    if value is None:
        return None
    value = value.strip()
    return value or None


def normalize_size(value: str | None) -> str | None:
    """Return the canonical T-shirt size, or ``None`` for an empty value."""
    value = _clean(value)
    if value is None:
        return None
    size = value.upper()
    if size not in TASK_SIZES:
        raise InvalidError(f"size must be one of {', '.join(TASK_SIZES)}: {value}")
    return size


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:100] or "project"


# --- blocking ---------------------------------------------------------------
# One link is one edge: this task waits for that task. The edges live in
# ``task_blockers`` and may cross projects. Everything a reader sees —
# ``blocked``, ``blockers``, ``blocking`` — is computed from the edges here
# and never stored, so the last open blocker going done unblocks a task with
# no further work.


def _task_ref(task: Task, project: Project | None) -> dict[str, Any]:
    """Enough of a linked task to show it and to open it."""
    return {
        "id": task.id,
        "title": task.title,
        "done": task.done,
        "project": {"name": project.name, "slug": project.slug} if project else None,
    }


async def _refs_by_id(
    session: AsyncSession, task_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any]]:
    """One display reference for each task id given, with its project."""
    if not task_ids:
        return {}
    tasks = list((await session.scalars(select(Task).where(Task.id.in_(task_ids)))).all())
    projects = {
        p.id: p
        for p in (
            await session.scalars(
                select(Project).where(Project.id.in_({t.project_id for t in tasks}))
            )
        ).all()
    }
    return {t.id: _task_ref(t, projects.get(t.project_id)) for t in tasks}


async def attach_blocks(session: AsyncSession, tasks: list[Task]) -> list[Task]:
    """Set the computed ``blocked``, ``blockers``, and ``blocking`` on each task.

    One pass for the whole list, whatever its length. ``blocked`` is true
    when at least one blocker is still open.
    """
    ids = {t.id for t in tasks}
    if not ids:
        return tasks
    edges = list(
        (
            await session.scalars(
                select(TaskBlocker)
                .where(or_(TaskBlocker.task_id.in_(ids), TaskBlocker.blocker_id.in_(ids)))
                .order_by(TaskBlocker.created_at)
            )
        ).all()
    )
    refs = await _refs_by_id(session, {e.task_id for e in edges} | {e.blocker_id for e in edges})
    blockers: dict[uuid.UUID, list[dict[str, Any]]] = {i: [] for i in ids}
    blocking: dict[uuid.UUID, list[dict[str, Any]]] = {i: [] for i in ids}
    for edge in edges:
        if edge.task_id in blockers and edge.blocker_id in refs:
            blockers[edge.task_id].append(refs[edge.blocker_id])
        if edge.blocker_id in blocking and edge.task_id in refs:
            blocking[edge.blocker_id].append(refs[edge.task_id])
    for task in tasks:
        task.blockers = blockers[task.id]
        task.blocking = blocking[task.id]
        task.blocked = any(not ref["done"] for ref in task.blockers)
    return tasks


async def blocked_ids(session: AsyncSession, task_ids: Collection[uuid.UUID]) -> set[uuid.UUID]:
    """Of the ids given, the ones that wait for at least one open blocker."""
    if not task_ids:
        return set()
    rows = await session.scalars(
        select(TaskBlocker.task_id)
        .join(Task, Task.id == TaskBlocker.blocker_id)
        .where(TaskBlocker.task_id.in_(task_ids), Task.done.is_(False))
    )
    return set(rows.all())


async def _titles(session: AsyncSession, task_ids: list[uuid.UUID]) -> list[str]:
    """The titles of ``task_ids``, in the order given."""
    rows = (await session.execute(select(Task.id, Task.title).where(Task.id.in_(task_ids)))).all()
    by_id = dict(rows)
    return [by_id.get(task_id, str(task_id)) for task_id in task_ids]


async def _reject_cycle(session: AsyncSession, task: Task, blocker: Task) -> None:
    """Refuse a link whose blocker already waits for ``task``, directly or not.

    Walks up the blockers of ``blocker``. A path back to ``task`` means the
    new link closes a loop that no order of work can satisfy. The error names
    every task in the loop.
    """
    blocks: dict[uuid.UUID, uuid.UUID] = {}  # a task -> the task it blocks
    frontier = [blocker.id]
    seen = {blocker.id}
    while frontier and task.id not in seen:
        rows = (
            await session.execute(
                select(TaskBlocker.task_id, TaskBlocker.blocker_id).where(
                    TaskBlocker.task_id.in_(frontier)
                )
            )
        ).all()
        frontier = []
        for waiting_id, upstream_id in rows:
            if upstream_id in seen:
                continue
            seen.add(upstream_id)
            blocks[upstream_id] = waiting_id
            frontier.append(upstream_id)
    if task.id not in seen:
        return
    loop = [task.id]
    while loop[-1] in blocks:
        loop.append(blocks[loop[-1]])
    titles = await _titles(session, loop)
    raise ConflictError("this link makes a loop: " + " blocks ".join([*titles, titles[0]]))


async def _waiting_project_ids(session: AsyncSession, task_id: uuid.UUID) -> set[str]:
    """The projects that hold a task which waits for ``task_id``."""
    rows = await session.scalars(
        select(Task.project_id)
        .join(TaskBlocker, TaskBlocker.task_id == Task.id)
        .where(TaskBlocker.blocker_id == task_id)
    )
    return {str(project_id) for project_id in rows.all()}


def _publish_tasks(project_ids: Iterable[str]) -> None:
    for project_id in project_ids:
        broadcaster.publish("tasks", project_id)


async def block_task(session: AsyncSession, task_id: uuid.UUID, blocker_id: uuid.UUID) -> Task:
    """Make a task wait for another task. The blocker can be in another project.

    A task cannot block itself, and a link that makes a loop is refused with
    an error that names the tasks in it. The same link twice changes nothing.
    """
    task = await get_task(session, task_id)
    blocker = await get_task(session, blocker_id)
    if task.id == blocker.id:
        raise ConflictError(f"a task cannot block itself: {task.title}")
    await _reject_cycle(session, task, blocker)
    existing = await session.scalar(
        select(TaskBlocker).where(
            TaskBlocker.task_id == task.id, TaskBlocker.blocker_id == blocker.id
        )
    )
    if existing is None:
        touched = {str(task.project_id), str(blocker.project_id)}
        session.add(TaskBlocker(task_id=task.id, blocker_id=blocker.id))
        await session.commit()
        _publish_tasks(touched)
    return (await attach_blocks(session, [task]))[0]


async def unblock_task(session: AsyncSession, task_id: uuid.UUID, blocker_id: uuid.UUID) -> Task:
    """Remove a blocking link. A link that is not there is no error."""
    task = await get_task(session, task_id)
    blocker = await get_task(session, blocker_id)
    existing = await session.scalar(
        select(TaskBlocker).where(
            TaskBlocker.task_id == task.id, TaskBlocker.blocker_id == blocker.id
        )
    )
    if existing is not None:
        touched = {str(task.project_id), str(blocker.project_id)}
        await session.delete(existing)
        await session.commit()
        _publish_tasks(touched)
    return (await attach_blocks(session, [task]))[0]


async def set_task_done(session: AsyncSession, task_id: uuid.UUID, done: bool) -> dict[str, Any]:
    """Mark a task done, or open, and say what that did to the links.

    The result holds the ``task``, ``unblocked`` (the tasks that now wait for
    nothing open, because this task was the last blocker they had), and
    ``warning`` (set when a task goes done while a blocker of its own is
    still open; it names those blockers). A still-blocked task is never
    refused: the work sometimes runs ahead of the links.
    """
    task = await get_task(session, task_id)
    await attach_blocks(session, [task])
    open_blockers = [ref for ref in task.blockers if not ref["done"]]
    waiting = list(task.blocking)
    task = await update_task(session, task_id, done=done)
    unblocked: list[dict[str, Any]] = []
    if done and waiting:
        still_blocked = await blocked_ids(session, [ref["id"] for ref in waiting])
        unblocked = [ref for ref in waiting if ref["id"] not in still_blocked]
    warning = None
    if done and open_blockers:
        names = ", ".join(ref["title"] for ref in open_blockers)
        warning = f"this task went done while it still waits for: {names}"
    return {"task": task, "unblocked": unblocked, "warning": warning}


# --- projects ---------------------------------------------------------------


async def _unique_slug(session: AsyncSession, name: str) -> str:
    base = _slugify(name)
    slug = base
    n = 2
    while await session.scalar(select(Project.id).where(Project.slug == slug)):
        slug = f"{base}-{n}"
        n += 1
    return slug


def roadmap_order(tasks: list[Task], groups: list[Group]) -> list[Task]:
    """Tasks in the order they are meant to be done: each phase, then the backlog."""
    rank = {g.id: index for index, g in enumerate(sorted(groups, key=lambda g: g.position))}
    backlog = len(rank)
    return sorted(
        tasks,
        key=lambda t: (
            rank.get(t.group_id, backlog) if t.group_id else backlog,
            t.position,
            t.created_at,
        ),
    )


def _health(
    project: Project,
    task_count: int,
    open_count: int,
    last_progress_at: datetime,
    today: date,
    ready_count: int,
) -> str | None:
    """The one-word state of a project, or ``None`` when there is nothing to say."""
    if project.status != "active":
        return project.status
    if not task_count:
        return None
    if task_count and not open_count and project.goal:
        return "ready_to_complete"
    if project.target_date and project.target_date < today and open_count:
        return "overdue"
    stalled_for = today - last_progress_at.astimezone(UTC).date()
    if project.goal and open_count and stalled_for >= timedelta(days=STALLED_AFTER_DAYS):
        return "stalled"
    # Every open task waits for another task: the project cannot move, which
    # is the same stall as no progress for 14 days.
    if (project.goal or project.target_date) and open_count and not ready_count:
        return "stalled"
    if project.goal or project.target_date:
        return "on_track"
    return None


def summarize(
    project: Project,
    tasks: list[Task],
    groups: list[Group],
    blocked: Collection[uuid.UUID] = (),
) -> dict[str, Any]:
    """A project with its computed fields. ``next_task`` is a ``Task`` or ``None``.

    ``blocked`` holds the ids of the tasks that wait for an open blocker (see
    ``blocked_ids``). Such a task is never the ``next_task``, and a project
    whose whole remaining work waits reads as ``stalled``.
    """
    task_count = len(tasks)
    done_count = sum(1 for t in tasks if t.done)
    open_count = task_count - done_count
    done_times = [t.done_at for t in tasks if t.done and t.done_at is not None]
    last_progress_at = max(done_times, default=project.created_at)
    open_in_order = [t for t in roadmap_order(tasks, groups) if not t.done and t.id not in blocked]
    return {
        "id": project.id,
        "name": project.name,
        "slug": project.slug,
        "goal": project.goal,
        "target_date": project.target_date,
        "status": project.status,
        "completed_at": project.completed_at,
        "created_at": project.created_at,
        "archived_at": project.archived_at,
        "task_count": task_count,
        "done_count": done_count,
        "open_count": open_count,
        "percent": done_count * 100 // task_count if task_count else 0,
        "health": _health(
            project, task_count, open_count, last_progress_at, _today(), len(open_in_order)
        ),
        "last_progress_at": last_progress_at,
        "next_task": open_in_order[0] if open_in_order else None,
    }


async def _tasks_and_groups_by_project(
    session: AsyncSession, project_ids: list[uuid.UUID]
) -> tuple[dict[uuid.UUID, list[Task]], dict[uuid.UUID, list[Group]]]:
    tasks: dict[uuid.UUID, list[Task]] = {pid: [] for pid in project_ids}
    groups: dict[uuid.UUID, list[Group]] = {pid: [] for pid in project_ids}
    if not project_ids:
        return tasks, groups
    for t in (await session.scalars(select(Task).where(Task.project_id.in_(project_ids)))).all():
        tasks[t.project_id].append(t)
    for g in (await session.scalars(select(Group).where(Group.project_id.in_(project_ids)))).all():
        groups[g.project_id].append(g)
    return tasks, groups


async def _blocked_in(session: AsyncSession, tasks: dict[uuid.UUID, list[Task]]) -> set[uuid.UUID]:
    """The blocked ids among the tasks of every project given."""
    return await blocked_ids(session, [t.id for group in tasks.values() for t in group])


async def _attach_next_tasks(session: AsyncSession, summaries: list[dict[str, Any]]) -> None:
    """Fill the computed block fields on each summary's ``next_task``."""
    await attach_blocks(session, [s["next_task"] for s in summaries if s["next_task"] is not None])


async def list_projects(
    session: AsyncSession, include_archived: bool = False, include_finished: bool = True
) -> list[dict]:
    """Project summaries, oldest first. ``include_finished=False`` drops done and closed."""
    query = select(Project).order_by(Project.created_at)
    if not include_archived:
        query = query.where(Project.archived_at.is_(None))
    if not include_finished:
        query = query.where(Project.status.not_in(FINISHED_STATUSES))
    projects = list((await session.scalars(query)).all())
    tasks, groups = await _tasks_and_groups_by_project(session, [p.id for p in projects])
    blocked = await _blocked_in(session, tasks)
    summaries = [summarize(p, tasks[p.id], groups[p.id], blocked) for p in projects]
    await _attach_next_tasks(session, summaries)
    return summaries


async def project_summary(session: AsyncSession, ref: str | uuid.UUID) -> dict[str, Any]:
    """One project's summary, by id, slug, or name."""
    project = await get_project(session, ref)
    tasks, groups = await _tasks_and_groups_by_project(session, [project.id])
    blocked = await _blocked_in(session, tasks)
    summary = summarize(project, tasks[project.id], groups[project.id], blocked)
    await _attach_next_tasks(session, [summary])
    return summary


async def next_steps(
    session: AsyncSession, ref: str | uuid.UUID | None = None, limit: int = 3
) -> list[dict[str, Any]]:
    """The next open tasks of every active project, or of one project.

    Without ``ref``, a paused or finished project is left out, and so is a
    project with nothing open. With ``ref``, that project is returned
    whatever its status. Each entry is a summary plus ``tasks``, the first
    ``limit`` open tasks in roadmap order.

    A task that waits for an open blocker is not a next step and is left
    out. A project whose whole remaining work waits keeps its place in the
    result, with an empty ``tasks`` list and the health ``stalled``.
    """
    if ref is not None:
        projects = [await get_project(session, ref)]
    else:
        projects = list(
            (
                await session.scalars(
                    select(Project)
                    .where(Project.archived_at.is_(None), Project.status == "active")
                    .order_by(Project.created_at)
                )
            ).all()
        )
    tasks, groups = await _tasks_and_groups_by_project(session, [p.id for p in projects])
    blocked = await _blocked_in(session, tasks)
    result = []
    for p in projects:
        open_in_order = [t for t in roadmap_order(tasks[p.id], groups[p.id]) if not t.done]
        if not open_in_order and ref is None:
            continue
        summary = summarize(p, tasks[p.id], groups[p.id], blocked)
        summary["tasks"] = [t for t in open_in_order if t.id not in blocked][: max(limit, 0)]
        result.append(summary)
    await attach_blocks(session, [t for s in result for t in s["tasks"]])
    await _attach_next_tasks(session, result)
    return result


async def get_project(session: AsyncSession, ref: str | uuid.UUID) -> Project:
    """Resolve a project by UUID, slug, or (case-insensitive) name.

    An id is tried first, then the slug, then the name. A name that matches
    more than one project is a conflict: use the slug or id.
    """
    if isinstance(ref, uuid.UUID):
        project = await session.get(Project, ref)
        if project is None:
            raise NotFoundError(f"project not found: {ref}")
        return project
    try:
        project = await session.get(Project, uuid.UUID(ref))
    except ValueError:
        project = None
    if project is None:
        project = await session.scalar(select(Project).where(Project.slug == ref))
    if project is None:
        matches = list(
            (
                await session.scalars(
                    select(Project).where(func.lower(Project.name) == ref.strip().lower())
                )
            ).all()
        )
        if len(matches) > 1:
            raise ConflictError(f"project name is ambiguous: {ref} (use the slug or id)")
        project = matches[0] if matches else None
    if project is None:
        raise NotFoundError(f"project not found: {ref}")
    return project


async def create_project(
    session: AsyncSession,
    name: str,
    goal: str | None = None,
    target_date: date | None = None,
) -> Project:
    project = Project(
        name=name.strip(),
        slug=await _unique_slug(session, name.strip()),
        goal=_clean(goal),
        target_date=target_date,
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)
    broadcaster.publish("projects")
    return project


async def update_project(
    session: AsyncSession,
    ref: str | uuid.UUID,
    name: str | None = None,
    archived: bool | None = None,
    goal: str | None = UNSET,
    target_date: date | None = UNSET,
) -> Project:
    """Change a project's name, archive flag, goal, or target date.

    ``goal`` and ``target_date`` clear when passed as ``None`` (or an empty
    goal) and stay when not passed. Use ``set_project_status`` for status.
    """
    project = await get_project(session, ref)
    if name is not None:
        project.name = name.strip()
    if archived is not None:
        project.archived_at = datetime.now(UTC) if archived else None
    if goal is not UNSET:
        project.goal = _clean(goal)
    if target_date is not UNSET:
        project.target_date = target_date
    await session.commit()
    await session.refresh(project)
    broadcaster.publish("projects")
    return project


async def set_project_status(session: AsyncSession, ref: str | uuid.UUID, status: str) -> Project:
    """Move a project to ``active``, ``paused``, ``done``, or ``closed``.

    ``done`` is refused while a task is open: finish, move, or delete the
    open tasks first, or use ``closed`` to end the project as it is.
    """
    if status not in PROJECT_STATUSES:
        raise InvalidError(f"status must be one of {', '.join(PROJECT_STATUSES)}: {status}")
    project = await get_project(session, ref)
    if status == "done":
        open_count = await session.scalar(
            select(func.count(Task.id)).where(Task.project_id == project.id, Task.done.is_(False))
        )
        if open_count:
            raise ConflictError(
                f"{open_count} task(s) are still open; finish them, or close the project instead"
            )
    project.status = status
    project.completed_at = datetime.now(UTC) if status in FINISHED_STATUSES else None
    await session.commit()
    await session.refresh(project)
    broadcaster.publish("projects")
    return project


async def complete_project(session: AsyncSession, ref: str | uuid.UUID) -> Project:
    """Mark a project ``done``. Refuses while a task is open."""
    return await set_project_status(session, ref, "done")


async def delete_project(session: AsyncSession, ref: str | uuid.UUID) -> None:
    project = await get_project(session, ref)
    await session.delete(project)
    await session.commit()
    broadcaster.publish("projects")


# --- groups -------------------------------------------------------------------
# Single-level grouping: a task is either ungrouped (project root list) or in
# exactly one group. Ordering is per-container — root tasks among themselves,
# a group's tasks within it, groups among themselves — so a project without
# groups behaves exactly as before.


async def resolve_group(
    session: AsyncSession, project_id: uuid.UUID, ref: str | uuid.UUID
) -> Group:
    """Resolve a group by UUID or (case-insensitive) name within a project."""
    if isinstance(ref, uuid.UUID):
        group = await session.get(Group, ref)
        if group is None or group.project_id != project_id:
            raise NotFoundError(f"group not found: {ref}")
        return group
    try:
        return await resolve_group(session, project_id, uuid.UUID(ref))
    except ValueError:
        pass
    matches = list(
        (
            await session.scalars(
                select(Group).where(
                    Group.project_id == project_id,
                    func.lower(Group.name) == ref.strip().lower(),
                )
            )
        ).all()
    )
    if not matches:
        raise NotFoundError(f"group not found: {ref}")
    if len(matches) > 1:
        raise ConflictError(f"group name is ambiguous: {ref} (use the id)")
    return matches[0]


async def list_groups(session: AsyncSession, ref: str | uuid.UUID) -> list[dict]:
    """Groups in display order, each with task/done counts."""
    project = await get_project(session, ref)
    rows = (
        await session.execute(
            select(
                Group,
                func.count(Task.id).label("task_count"),
                func.count(Task.id).filter(Task.done.is_(True)).label("done_count"),
            )
            .outerjoin(Task, Task.group_id == Group.id)
            .where(Group.project_id == project.id)
            .group_by(Group.id)
            .order_by(Group.position, Group.created_at)
        )
    ).all()
    return [
        {
            "id": g.id,
            "project_id": g.project_id,
            "name": g.name,
            "position": g.position,
            "target_date": g.target_date,
            "created_at": g.created_at,
            "updated_at": g.updated_at,
            "task_count": task_count,
            "done_count": done_count,
            "percent": done_count * 100 // task_count if task_count else 0,
        }
        for g, task_count, done_count in rows
    ]


async def create_group(session: AsyncSession, ref: str | uuid.UUID, name: str) -> Group:
    project = await get_project(session, ref)
    current_max = await session.scalar(
        select(func.max(Group.position)).where(Group.project_id == project.id)
    )
    group = Group(
        project_id=project.id,
        name=name.strip(),
        position=(current_max or 0.0) + POSITION_STEP,
    )
    session.add(group)
    await session.commit()
    await session.refresh(group)
    broadcaster.publish("tasks", str(group.project_id))
    return group


async def get_group(session: AsyncSession, group_id: uuid.UUID) -> Group:
    group = await session.get(Group, group_id)
    if group is None:
        raise NotFoundError(f"group not found: {group_id}")
    return group


async def update_group(
    session: AsyncSession,
    group_id: uuid.UUID,
    name: str | None = None,
    target_date: date | None = UNSET,
) -> Group:
    """Change a group's name or target date (``None`` clears the date)."""
    group = await get_group(session, group_id)
    if name is not None:
        group.name = name.strip()
    if target_date is not UNSET:
        group.target_date = target_date
    await session.commit()
    await session.refresh(group)
    broadcaster.publish("tasks", str(group.project_id))
    return group


async def rename_group(session: AsyncSession, group_id: uuid.UUID, name: str) -> Group:
    return await update_group(session, group_id, name=name)


async def delete_group(session: AsyncSession, group_id: uuid.UUID) -> None:
    """Delete a group; its tasks are ungrouped (appended to the root list)."""
    group = await get_group(session, group_id)
    members = list(
        (
            await session.scalars(
                select(Task)
                .where(Task.group_id == group_id)
                .order_by(Task.position, Task.created_at)
            )
        ).all()
    )
    root_max = await session.scalar(
        select(func.max(Task.position)).where(
            Task.project_id == group.project_id, Task.group_id.is_(None)
        )
    )
    base = root_max or 0.0
    project_id = str(group.project_id)
    for j, task in enumerate(members):
        task.group_id = None
        task.position = base + (j + 1) * POSITION_STEP
    await session.delete(group)
    await session.commit()
    broadcaster.publish("tasks", project_id)


async def reorder_groups(
    session: AsyncSession, ref: str | uuid.UUID, ordered_ids: list[uuid.UUID]
) -> list[dict]:
    """Authoritative reorder of a project's groups."""
    project = await get_project(session, ref)
    groups = {
        g.id: g
        for g in (await session.scalars(select(Group).where(Group.project_id == project.id))).all()
    }
    if set(ordered_ids) != set(groups):
        raise ConflictError("ordered_ids must contain exactly the project's group ids")
    for index, group_id in enumerate(ordered_ids):
        groups[group_id].position = (index + 1) * POSITION_STEP
    await session.commit()
    broadcaster.publish("tasks", str(project.id))
    return await list_groups(session, project.id)


# --- tasks ------------------------------------------------------------------


async def list_tasks(
    session: AsyncSession, ref: str | uuid.UUID, include_done: bool = True
) -> list[Task]:
    """A project's tasks in stored order, each with its computed block fields."""
    project = await get_project(session, ref)
    query = (
        select(Task).where(Task.project_id == project.id).order_by(Task.position, Task.created_at)
    )
    if not include_done:
        query = query.where(Task.done.is_(False))
    return await attach_blocks(session, list((await session.scalars(query)).all()))


async def get_task(session: AsyncSession, task_id: uuid.UUID) -> Task:
    task = await session.get(Task, task_id)
    if task is None:
        raise NotFoundError(f"task not found: {task_id}")
    return task


async def _next_position(
    session: AsyncSession, project_id: uuid.UUID, group_id: uuid.UUID | None = None
) -> float:
    """Next append position within a container (root or one group)."""
    current_max = await session.scalar(
        select(func.max(Task.position)).where(
            Task.project_id == project_id,
            Task.group_id == group_id if group_id else Task.group_id.is_(None),
        )
    )
    return (current_max or 0.0) + POSITION_STEP


async def create_task(
    session: AsyncSession,
    ref: str | uuid.UUID,
    title: str,
    notes: str = "",
    group: str | uuid.UUID | None = None,
    size: str | None = None,
    due_date: date | None = None,
    assignee: str | None = None,
) -> Task:
    project = await get_project(session, ref)
    group_id = (await resolve_group(session, project.id, group)).id if group is not None else None
    task = Task(
        project_id=project.id,
        group_id=group_id,
        title=title.strip(),
        notes=notes,
        size=normalize_size(size),
        due_date=due_date,
        assignee=_clean(assignee),
        position=await _next_position(session, project.id, group_id),
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    broadcaster.publish("tasks", str(task.project_id))
    return task


async def set_task_group(
    session: AsyncSession, task_id: uuid.UUID, group: str | uuid.UUID | None
) -> Task:
    """Move a task into a group (or out, with group=None); appended at the end."""
    task = await get_task(session, task_id)
    group_id = (
        (await resolve_group(session, task.project_id, group)).id if group is not None else None
    )
    if group_id != task.group_id:
        task.group_id = group_id
        task.position = await _next_position(session, task.project_id, group_id)
    await session.commit()
    await session.refresh(task)
    broadcaster.publish("tasks", str(task.project_id))
    return (await attach_blocks(session, [task]))[0]


async def update_task(
    session: AsyncSession,
    task_id: uuid.UUID,
    title: str | None = None,
    notes: str | None = None,
    done: bool | None = None,
    size: str | None = UNSET,
    due_date: date | None = UNSET,
    assignee: str | None = UNSET,
) -> Task:
    """Edit a task. ``size``, ``due_date``, and ``assignee`` clear on ``None``.

    A change of ``done`` also tells the projects of the tasks that wait for
    this one: their blocked state is computed from it. Use ``set_task_done``
    to get that list back as well.
    """
    task = await get_task(session, task_id)
    if title is not None:
        task.title = title.strip()
    if notes is not None:
        task.notes = notes
    done_changed = done is not None and done != task.done
    if done_changed:
        task.done = done
        task.done_at = datetime.now(UTC) if done else None
    if size is not UNSET:
        task.size = normalize_size(size)
    if due_date is not UNSET:
        task.due_date = due_date
    if assignee is not UNSET:
        task.assignee = _clean(assignee)
    waiting_projects = await _waiting_project_ids(session, task.id) if done_changed else set()
    await session.commit()
    await session.refresh(task)
    broadcaster.publish("tasks", str(task.project_id))
    _publish_tasks(waiting_projects - {str(task.project_id)})
    return (await attach_blocks(session, [task]))[0]


async def delete_task(session: AsyncSession, task_id: uuid.UUID) -> None:
    task = await get_task(session, task_id)
    project_id = str(task.project_id)
    await session.delete(task)
    await session.commit()
    broadcaster.publish("tasks", project_id)


async def reorder_tasks(
    session: AsyncSession,
    ref: str | uuid.UUID,
    ordered_ids: list[uuid.UUID],
    group: str | uuid.UUID | None = None,
) -> list[Task]:
    """Authoritative reorder of ONE container (the root list, or one group).

    ordered_ids becomes the container's complete content, in order. Listed
    tasks currently in another container are moved in (this is how a drag
    into a group lands in a single call). A task currently in the container
    but missing from the list is a conflict — a stale client (missing a task
    another client just added) must refetch instead of silently dropping it.

    For a project with no groups this is identical to the original whole-
    project reorder.
    """
    project = await get_project(session, ref)
    group_id = (await resolve_group(session, project.id, group)).id if group is not None else None
    tasks = {
        t.id: t
        for t in (await session.scalars(select(Task).where(Task.project_id == project.id))).all()
    }
    unknown = [i for i in ordered_ids if i not in tasks]
    if unknown or len(set(ordered_ids)) != len(ordered_ids):
        raise ConflictError("ordered_ids must be distinct task ids of this project")
    current_members = {i for i, t in tasks.items() if t.group_id == group_id}
    missing = current_members - set(ordered_ids)
    if missing:
        raise ConflictError("ordered_ids must include every task currently in this container")
    for index, task_id in enumerate(ordered_ids):
        tasks[task_id].group_id = group_id
        tasks[task_id].position = (index + 1) * POSITION_STEP
    project_id = project.id
    await session.commit()
    broadcaster.publish("tasks", str(project_id))
    # Re-select with populate_existing: updated_at is server-generated on
    # UPDATE, so the in-memory objects hold expired attributes that can't
    # lazy-load during async response serialization.
    reordered = (
        await session.scalars(
            select(Task)
            .where(
                Task.project_id == project_id,
                Task.group_id == group_id if group_id else Task.group_id.is_(None),
            )
            .order_by(Task.position, Task.created_at)
            .execution_options(populate_existing=True)
        )
    ).all()
    return await attach_blocks(session, list(reordered))


async def move_task(session: AsyncSession, task_id: uuid.UUID, to_project: str | uuid.UUID) -> Task:
    """Move a task to the end of another project's root list (ungrouped)."""
    task = await get_task(session, task_id)
    project = await get_project(session, to_project)
    source_project_id = str(task.project_id)
    task.project_id = project.id
    task.group_id = None
    task.position = await _next_position(session, project.id)
    await session.commit()
    await session.refresh(task)
    broadcaster.publish("tasks", source_project_id)
    broadcaster.publish("tasks", str(task.project_id))
    return (await attach_blocks(session, [task]))[0]


# --- search -------------------------------------------------------------------


async def search(
    session: AsyncSession,
    query: str,
    include_done: bool = False,
    project: str | uuid.UUID | None = None,
    limit: int = SEARCH_LIMIT,
) -> dict[str, Any]:
    """Find the projects and the tasks that hold ``query``.

    The match is a case-insensitive substring: a project matches on its
    name or its goal, a task on its title or its notes. An archived project
    is never searched, and neither are its tasks. By default only open
    tasks match, and only the projects that ``list_projects`` shows with
    ``include_finished=False``. With ``include_done=True`` a done task
    matches too, and a done or closed project as well. ``project`` (an id,
    a slug, or a name) narrows the task match to one project.

    The result has ``projects``, project summaries oldest first, and
    ``tasks``, one entry for each task with the ``task``, the summary of
    its ``project``, and its ``group`` (``None`` for a task in the
    backlog). Each task carries its computed block fields. The tasks come
    project by project, in the same project order, and in roadmap order
    inside a project. ``limit`` caps each list. An empty query matches
    nothing.
    """
    needle = query.strip().lower()
    if not needle or limit <= 0:
        return {"projects": [], "tasks": []}
    only = (await get_project(session, project)).id if project is not None else None
    searchable = select(Project).where(Project.archived_at.is_(None)).order_by(Project.created_at)
    if not include_done:
        searchable = searchable.where(Project.status.not_in(FINISHED_STATUSES))
    projects = list((await session.scalars(searchable)).all())
    tasks, groups = await _tasks_and_groups_by_project(session, [p.id for p in projects])
    blocked = await _blocked_in(session, tasks)
    summaries = {p.id: summarize(p, tasks[p.id], groups[p.id], blocked) for p in projects}
    found_projects = [
        summaries[p.id]
        for p in projects
        if needle in p.name.lower() or (p.goal is not None and needle in p.goal.lower())
    ]
    found_tasks: list[dict[str, Any]] = []
    for p in projects:
        if only is not None and p.id != only:
            continue
        by_id = {g.id: g for g in groups[p.id]}
        for task in roadmap_order(tasks[p.id], groups[p.id]):
            if task.done and not include_done:
                continue
            if needle not in task.title.lower() and needle not in task.notes.lower():
                continue
            found_tasks.append(
                {
                    "task": task,
                    "project": summaries[p.id],
                    "group": by_id.get(task.group_id) if task.group_id else None,
                }
            )
    found_projects = found_projects[:limit]
    found_tasks = found_tasks[:limit]
    await attach_blocks(session, [f["task"] for f in found_tasks])
    await _attach_next_tasks(session, list(summaries.values()))
    return {"projects": found_projects, "tasks": found_tasks}
