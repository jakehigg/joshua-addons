"""Model storage: UUID round trip, UTC timestamps, and the foreign-key actions.

Runs on every engine the ``engine`` fixture offers, so cascade and set-null
are proved on SQLite (FK pragma) and, with ``TEST_DATABASE_URL``, on Postgres.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from joshua_tasks import service
from joshua_tasks.models import Group, Project, Task
from sqlalchemy import func, select, text


async def test_ids_round_trip_as_the_same_uuid_string(db) -> None:
    fixed = uuid.UUID("12345678-1234-5678-1234-567812345678")
    db.add(Project(id=fixed, name="Fixed", slug="fixed"))
    await db.commit()
    db.expire_all()
    project = await db.get(Project, fixed)
    assert project is not None
    assert isinstance(project.id, uuid.UUID)
    assert str(project.id) == "12345678-1234-5678-1234-567812345678"
    # Stored as the 36-character hyphenated string on every dialect.
    raw = (await db.execute(text("SELECT id FROM projects"))).scalar_one()
    assert raw == "12345678-1234-5678-1234-567812345678"


async def test_guid_accepts_string_ids_in_queries(db) -> None:
    p = await service.create_project(db, "Q")
    found = await db.scalar(select(Project).where(Project.id == str(p.id)))
    assert found is not None and found.id == p.id


async def test_timestamps_are_timezone_aware_utc(db) -> None:
    await service.create_project(db, "T")
    t = await service.create_task(db, "t", "task")
    t = await service.update_task(db, t.id, done=True)
    task_id = t.id
    db.expire_all()
    task = await service.get_task(db, task_id)
    await db.refresh(task)
    for value in (task.created_at, task.updated_at, task.done_at):
        assert value is not None
        assert value.tzinfo is not None and value.utcoffset().total_seconds() == 0
    assert abs((datetime.now(UTC) - task.created_at).total_seconds()) < 60


async def test_updated_at_moves_on_update(db) -> None:
    await service.create_project(db, "U")
    t = await service.create_task(db, "u", "task")
    before = t.updated_at
    t = await service.update_task(db, t.id, notes="changed")
    assert t.updated_at > before


async def test_deleting_a_project_cascades_to_tasks_and_groups(db) -> None:
    p = await service.create_project(db, "Cascade")
    await service.create_group(db, "cascade", "G")
    await service.create_task(db, "cascade", "a", group="G")
    await service.create_task(db, "cascade", "b")
    await service.delete_project(db, p.id)
    assert await db.scalar(select(func.count(Task.id))) == 0
    assert await db.scalar(select(func.count(Group.id))) == 0


async def test_deleting_a_group_row_sets_task_group_to_null(db) -> None:
    """The DB-level action itself, bypassing the service's ungroup logic."""
    await service.create_project(db, "SetNull")
    g = await service.create_group(db, "setnull", "G")
    t = await service.create_task(db, "setnull", "a", group="G")
    task_id = t.id
    await db.execute(text("DELETE FROM groups WHERE id = :id"), {"id": str(g.id)})
    await db.commit()
    db.expire_all()
    task = await service.get_task(db, task_id)
    assert task.group_id is None
