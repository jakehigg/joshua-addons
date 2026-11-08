"""``run_migrations`` creates the tables, adds missing columns, and is idempotent."""

from __future__ import annotations

from joshua_tasks.database import build_engine
from joshua_tasks.migrations import ADDITIVE_INDEXES, ADDITIVE_MIGRATIONS, run_migrations
from joshua_tasks.models import Base
from sqlalchemy import inspect, text


def _columns(conn, table: str) -> set[str]:
    return {c["name"] for c in inspect(conn).get_columns(table)}


def _indexes(conn, table: str) -> dict[str, bool]:
    return {i["name"]: bool(i["unique"]) for i in inspect(conn).get_indexes(table)}


async def test_run_migrations_is_idempotent(tmp_path) -> None:
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    try:
        await run_migrations(engine)
        await run_migrations(engine)
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
            indexes = await conn.run_sync(lambda c: _indexes(c, "tasks"))
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
        ("tasks", "number"),
    ]
    assert [(i.table, i.column) for i in ADDITIVE_INDEXES] == [("tasks", "number")]
    assert set(Base.metadata.tables) == {"projects", "groups", "tasks", "task_numbers"}
    assert tables == {"projects", "groups", "tasks", "task_numbers"}
    assert indexes["ix_tasks_number"] is True


async def test_a_first_release_database_gets_the_new_columns(tmp_path) -> None:
    """A database from before issue #37 gains every column, and its rows a status."""
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    try:
        await run_migrations(engine)
        async with engine.begin() as conn:
            # SQLite refuses to drop a column an index uses, so the index goes first.
            for i in ADDITIVE_INDEXES:
                await conn.execute(text(f"DROP INDEX {i.name}"))
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
    assert {"size", "due_date", "assignee", "number"} <= tasks
    assert status == "active"


# One project, and three tasks whose ids do not follow their creation order.
PROJECT_ID = "00000000-0000-0000-0000-000000000001"
OLD_TASKS = [
    ("00000000-0000-0000-0000-0000000000c0", "third", "2024-03-01 00:00:00.000000"),
    ("00000000-0000-0000-0000-0000000000a0", "first", "2024-01-01 00:00:00.000000"),
    ("00000000-0000-0000-0000-0000000000b0", "second", "2024-02-01 00:00:00.000000"),
]


async def test_the_tasks_of_an_old_database_are_numbered_oldest_first(tmp_path) -> None:
    """A database from before issue #55 numbers its tasks in creation order."""
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'number.db'}")
    try:
        await run_migrations(engine)
        async with engine.begin() as conn:
            # Back to the schema before the number: no index, no column, and
            # no sequence row.
            await conn.execute(text("DROP INDEX ix_tasks_number"))
            await conn.execute(text("ALTER TABLE tasks DROP COLUMN number"))
            await conn.execute(text("DELETE FROM task_numbers"))
            await conn.execute(
                text("INSERT INTO projects (id, name, slug) VALUES (:id, 'Old', 'old')"),
                {"id": PROJECT_ID},
            )
            for index, (task_id, title, created_at) in enumerate(OLD_TASKS):
                await conn.execute(
                    text(
                        "INSERT INTO tasks "
                        "(id, project_id, title, notes, done, position, created_at, updated_at) "
                        "VALUES (:id, :project_id, :title, '', 0, :position, :at, :at)"
                    ),
                    {
                        "id": task_id,
                        "project_id": PROJECT_ID,
                        "title": title,
                        "position": (index + 1) * 1024.0,
                        "at": created_at,
                    },
                )

        await run_migrations(engine)

        async with engine.connect() as conn:
            numbered = (
                await conn.execute(text("SELECT title, number FROM tasks ORDER BY number"))
            ).all()
            last = (await conn.execute(text("SELECT last_number FROM task_numbers"))).scalar_one()
            indexes = await conn.run_sync(lambda c: _indexes(c, "tasks"))
    finally:
        await engine.dispose()
    assert [tuple(row) for row in numbered] == [("first", 1), ("second", 2), ("third", 3)]
    # The sequence goes on above the numbers the backfill gave.
    assert last == 3
    assert indexes["ix_tasks_number"] is True
