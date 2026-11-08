"""Service layer — the single source of truth for all task/project operations.

Both the REST API (joshua_tasks.api) and the MCP tools (joshua_tasks.server) call these
functions, so behavior can never drift between the two surfaces. Every
function takes an AsyncSession and commits its own work.

Ordering: tasks carry a float `position` (smaller = higher). Appends go to
max+STEP; a reorder is authoritative and rewrites the whole project's
positions as (index+1)*STEP, so precision never degrades.
"""

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .events import broadcaster
from .models import Group, Project, Task

POSITION_STEP = 1024.0


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:100] or "project"


# --- projects ---------------------------------------------------------------


async def _unique_slug(session: AsyncSession, name: str) -> str:
    base = _slugify(name)
    slug = base
    n = 2
    while await session.scalar(select(Project.id).where(Project.slug == slug)):
        slug = f"{base}-{n}"
        n += 1
    return slug


async def list_projects(session: AsyncSession, include_archived: bool = False) -> list[dict]:
    """Projects with task/done counts, oldest first."""
    query = (
        select(
            Project,
            func.count(Task.id).label("task_count"),
            func.count(Task.id).filter(Task.done.is_(True)).label("done_count"),
        )
        .outerjoin(Task)
        .group_by(Project.id)
        .order_by(Project.created_at)
    )
    if not include_archived:
        query = query.where(Project.archived_at.is_(None))
    rows = (await session.execute(query)).all()
    return [
        {
            "id": p.id,
            "name": p.name,
            "slug": p.slug,
            "created_at": p.created_at,
            "archived_at": p.archived_at,
            "task_count": task_count,
            "done_count": done_count,
        }
        for p, task_count, done_count in rows
    ]


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


async def create_project(session: AsyncSession, name: str) -> Project:
    project = Project(name=name.strip(), slug=await _unique_slug(session, name.strip()))
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
) -> Project:
    project = await get_project(session, ref)
    if name is not None:
        project.name = name.strip()
    if archived is not None:
        project.archived_at = datetime.now(UTC) if archived else None
    await session.commit()
    await session.refresh(project)
    broadcaster.publish("projects")
    return project


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
            "created_at": g.created_at,
            "updated_at": g.updated_at,
            "task_count": task_count,
            "done_count": done_count,
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


async def rename_group(session: AsyncSession, group_id: uuid.UUID, name: str) -> Group:
    group = await get_group(session, group_id)
    group.name = name.strip()
    await session.commit()
    await session.refresh(group)
    broadcaster.publish("tasks", str(group.project_id))
    return group


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
    project = await get_project(session, ref)
    query = (
        select(Task).where(Task.project_id == project.id).order_by(Task.position, Task.created_at)
    )
    if not include_done:
        query = query.where(Task.done.is_(False))
    return list((await session.scalars(query)).all())


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
) -> Task:
    project = await get_project(session, ref)
    group_id = (await resolve_group(session, project.id, group)).id if group is not None else None
    task = Task(
        project_id=project.id,
        group_id=group_id,
        title=title.strip(),
        notes=notes,
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
    return task


async def update_task(
    session: AsyncSession,
    task_id: uuid.UUID,
    title: str | None = None,
    notes: str | None = None,
    done: bool | None = None,
) -> Task:
    task = await get_task(session, task_id)
    if title is not None:
        task.title = title.strip()
    if notes is not None:
        task.notes = notes
    if done is not None and done != task.done:
        task.done = done
        task.done_at = datetime.now(UTC) if done else None
    await session.commit()
    await session.refresh(task)
    broadcaster.publish("tasks", str(task.project_id))
    return task


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
    return list(reordered)


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
    return task
