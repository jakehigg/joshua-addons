"""Additive schema migrations for the tasks addon.

No Alembic. ``run_migrations`` first calls ``Base.metadata.create_all``, which
creates any table that is missing entirely, with every column the current
models define. It then walks ``ADDITIVE_MIGRATIONS`` in order and issues
``ALTER TABLE ... ADD COLUMN`` for any column an existing table does not have
yet, found by SQLAlchemy inspection rather than a dialect check — the same
call runs unchanged on SQLite and Postgres.

A release never drops a table or a column. To add a new column: add it to the
model in ``models.py``, then append one ``Migration`` entry here naming the
table, the column, and its SQLAlchemy type. A fresh database gets the column
from ``create_all``; an existing database picks it up as an additive
``ALTER TABLE`` on its next start.

``ADDITIVE_INDEXES`` does the same for an index a new column needs, because
``create_all`` builds an index for a new table only. ``_number_tasks`` then
gives the tasks of an existing database their numbers, before the unique
index over them is built.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Date, Integer, String, Text, func, inspect, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.types import TypeEngine

from .models import TASK_NUMBER_ID, Base, Task, TaskNumber, UTCDateTime


@dataclass(frozen=True)
class Migration:
    """One additive column, applied when its table exists but the column does not.

    ``references``, when set, is a raw ``REFERENCES`` clause (for example
    ``"projects(id) ON DELETE SET NULL"``) appended to the ``ADD COLUMN``
    statement. ``constraint_name``, when set, names that foreign key with an
    explicit ``CONSTRAINT`` clause; give it whenever ``models.py`` names the
    same constraint, so an additive-path database and a ``create_all`` one end
    up with the same constraint name.
    """

    table: str
    column: str
    type_: TypeEngine
    references: str | None = None
    constraint_name: str | None = None
    # ``server_default`` is a SQL literal (quote a string yourself). A column
    # with ``nullable=False`` must give one, so existing rows get a value.
    server_default: str | None = None
    nullable: bool = True


# Ordered oldest to newest. The first release's schema is whatever
# ``create_all`` builds. Append one ``Migration`` per column added after it.
ADDITIVE_MIGRATIONS: list[Migration] = [
    # Goals, phases, and task details (issue #37).
    Migration("projects", "goal", Text()),
    Migration("projects", "target_date", Date()),
    Migration("projects", "status", String(16), server_default="'active'", nullable=False),
    Migration("projects", "completed_at", UTCDateTime()),
    Migration("groups", "target_date", Date()),
    Migration("tasks", "size", String(4)),
    Migration("tasks", "due_date", Date()),
    Migration("tasks", "assignee", Text()),
    # The short task number (issue #55). The column arrives nullable here,
    # because SQLite refuses a NOT NULL column without a default and one
    # default for every row would break the unique index. ``_number_tasks``
    # fills every row in the same run.
    Migration("tasks", "number", Integer()),
]


@dataclass(frozen=True)
class UniqueIndex:
    """One unique index, created when its table and its column exist.

    ``models.py`` names the same index, so a database on the additive path
    and a ``create_all`` one end up with the same index.
    """

    name: str
    table: str
    column: str


# Ordered oldest to newest, as ``ADDITIVE_MIGRATIONS``.
ADDITIVE_INDEXES: list[UniqueIndex] = [
    UniqueIndex("ix_tasks_number", "tasks", "number"),
]


async def _existing_columns(conn: AsyncConnection, table: str) -> set[str]:
    def _inspect(sync_conn) -> set[str]:
        inspector = inspect(sync_conn)
        if not inspector.has_table(table):
            return set()
        return {col["name"] for col in inspector.get_columns(table)}

    return await conn.run_sync(_inspect)


async def _number_tasks(conn: AsyncConnection) -> None:
    """Give a number to every task that has none, and hold the sequence above them.

    A database from before the task number has tasks and no numbers. They
    get 1, 2, 3 ... in creation order, so the numbering says which task came
    first. The sequence row then holds the highest number given, and
    ``service.create_task`` goes on from there. A run with nothing to number
    changes nothing.
    """
    tasks = Base.metadata.tables[Task.__tablename__]
    numbers = Base.metadata.tables[TaskNumber.__tablename__]
    if "number" not in await _existing_columns(conn, Task.__tablename__):
        return
    highest = await conn.scalar(select(func.max(tasks.c.number))) or 0
    unnumbered = (
        await conn.scalars(
            select(tasks.c.id)
            .where(tasks.c.number.is_(None))
            .order_by(tasks.c.created_at, tasks.c.id)
        )
    ).all()
    for offset, task_id in enumerate(unnumbered, start=1):
        await conn.execute(
            tasks.update().where(tasks.c.id == task_id).values(number=highest + offset)
        )
    last = highest + len(unnumbered)
    current = await conn.scalar(select(numbers.c.last_number).where(numbers.c.id == TASK_NUMBER_ID))
    if current is None:
        await conn.execute(numbers.insert().values(id=TASK_NUMBER_ID, last_number=last))
    elif current < last:
        await conn.execute(
            numbers.update().where(numbers.c.id == TASK_NUMBER_ID).values(last_number=last)
        )


async def run_migrations(engine: AsyncEngine) -> None:
    """Create any missing table, add any missing column, then number the tasks."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with engine.begin() as conn:
        for migration in ADDITIVE_MIGRATIONS:
            existing = await _existing_columns(conn, migration.table)
            if not existing or migration.column in existing:
                continue
            compiled_type = migration.type_.compile(dialect=conn.dialect)
            stmt = f"ALTER TABLE {migration.table} ADD COLUMN {migration.column} {compiled_type}"
            if migration.server_default is not None:
                stmt += f" DEFAULT {migration.server_default}"
            if not migration.nullable:
                stmt += " NOT NULL"
            if migration.references:
                if migration.constraint_name:
                    stmt += f" CONSTRAINT {migration.constraint_name}"
                stmt += f" REFERENCES {migration.references}"
            # migration.table/column/type_/references/constraint_name come
            # only from ADDITIVE_MIGRATIONS above, a fixed list in this file,
            # never
            # from a caller or request.
            await conn.exec_driver_sql(stmt)

    async with engine.begin() as conn:
        await _number_tasks(conn)

    async with engine.begin() as conn:
        for index in ADDITIVE_INDEXES:
            if index.column not in await _existing_columns(conn, index.table):
                continue
            # index.name/table/column come only from ADDITIVE_INDEXES above.
            await conn.exec_driver_sql(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {index.name} "
                f"ON {index.table} ({index.column})"
            )
