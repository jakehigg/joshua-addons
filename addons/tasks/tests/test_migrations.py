"""``run_migrations`` creates the three tables, adds missing columns, and is idempotent."""

from __future__ import annotations

from joshua_tasks.database import build_engine
from joshua_tasks.migrations import ADDITIVE_MIGRATIONS, run_migrations
from joshua_tasks.models import Base
from sqlalchemy import inspect, text


def _columns(conn, table: str) -> set[str]:
    return {c["name"] for c in inspect(conn).get_columns(table)}


async def test_run_migrations_is_idempotent(tmp_path) -> None:
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    try:
        await run_migrations(engine)
        await run_migrations(engine)
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
    finally:
        await engine.dispose()
    assert [(m.table, m.column) for m in ADDITIVE_MIGRATIONS] == [
        ("projects", "goal"),
        ("projects", "target_date"),
        ("projects", "status"),
        ("projects", "completed_at"),
        ("groups", "target_date"),
        ("tasks", "size"),
        ("tasks", "due_date"),
        ("tasks", "assignee"),
    ]
    assert set(Base.metadata.tables) == {"projects", "groups", "tasks"}
    assert tables == {"projects", "groups", "tasks"}


async def test_a_first_release_database_gets_the_new_columns(tmp_path) -> None:
    """A database from before issue #37 gains every column, and its rows a status."""
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    try:
        await run_migrations(engine)
        async with engine.begin() as conn:
            for m in ADDITIVE_MIGRATIONS:
                await conn.execute(text(f"ALTER TABLE {m.table} DROP COLUMN {m.column}"))
            await conn.execute(
                text(
                    "INSERT INTO projects (id, name, slug) "
                    "VALUES ('00000000-0000-0000-0000-000000000001', 'Old', 'old')"
                )
            )
        async with engine.connect() as conn:
            before = await conn.run_sync(lambda c: _columns(c, "projects"))
        assert "status" not in before and "goal" not in before

        await run_migrations(engine)

        async with engine.connect() as conn:
            projects = await conn.run_sync(lambda c: _columns(c, "projects"))
            groups = await conn.run_sync(lambda c: _columns(c, "groups"))
            tasks = await conn.run_sync(lambda c: _columns(c, "tasks"))
            status = (await conn.execute(text("SELECT status FROM projects"))).scalar_one()
    finally:
        await engine.dispose()
    assert {"goal", "target_date", "status", "completed_at"} <= projects
    assert "target_date" in groups
    assert {"size", "due_date", "assignee"} <= tasks
    assert status == "active"
