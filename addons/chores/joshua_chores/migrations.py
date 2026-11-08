"""Additive schema migrations for the chores addon.

No Alembic. ``run_migrations`` first calls ``Base.metadata.create_all``. That
call creates each table that does not exist, with every column the current
models define. Then ``run_migrations`` walks ``ADDITIVE_MIGRATIONS`` in order
and issues ``ALTER TABLE ... ADD COLUMN`` for each column that an existing
table does not have. It finds the columns by SQLAlchemy inspection, not by a
dialect check, so the same call runs on SQLite and on Postgres.

A release never drops a table or a column. To add a column: add it to the
model in ``models.py``, then append one ``Migration`` entry here with the
table, the column, and its SQLAlchemy type. A fresh database gets the column
from ``create_all``. An existing database gets it from an additive
``ALTER TABLE`` at its next start.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.types import TypeEngine

from .models import Base


@dataclass(frozen=True)
class Migration:
    """One additive column. It applies when its table exists and the column does not.

    ``references``, when set, is a raw ``REFERENCES`` clause (for example
    ``"members(id) ON DELETE SET NULL"``) that follows the ``ADD COLUMN``
    statement. SQLite and Postgres both accept an inline ``REFERENCES`` clause
    on ``ALTER TABLE ... ADD COLUMN``. Each existing row gets NULL in the new
    column, and NULL always satisfies a foreign key.

    ``constraint_name``, when set, gives the foreign key an explicit
    ``CONSTRAINT`` name. Set it when ``models.py`` names the same constraint.
    If you do not, Postgres makes its own name from the inline clause, and a
    database that took the additive path then has a different constraint name
    than a database that ``create_all`` built.
    """

    table: str
    column: str
    type_: TypeEngine
    references: str | None = None
    constraint_name: str | None = None


# Ordered oldest to newest.
ADDITIVE_MIGRATIONS: list[Migration] = []


async def _existing_columns(conn: AsyncConnection, table: str) -> set[str]:
    def _inspect(sync_conn) -> set[str]:
        inspector = inspect(sync_conn)
        if not inspector.has_table(table):
            return set()
        return {col["name"] for col in inspector.get_columns(table)}

    return await conn.run_sync(_inspect)


async def run_migrations(engine: AsyncEngine, migrations: list[Migration] | None = None) -> None:
    """Create each missing table, then add each missing additive column.

    ``migrations`` defaults to ``ADDITIVE_MIGRATIONS``. A test gives its own
    list to exercise the mechanism.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    pending = ADDITIVE_MIGRATIONS if migrations is None else migrations
    async with engine.begin() as conn:
        for migration in pending:
            existing = await _existing_columns(conn, migration.table)
            if not existing or migration.column in existing:
                continue
            compiled_type = migration.type_.compile(dialect=conn.dialect)
            stmt = f"ALTER TABLE {migration.table} ADD COLUMN {migration.column} {compiled_type}"
            if migration.references:
                if migration.constraint_name:
                    stmt += f" CONSTRAINT {migration.constraint_name}"
                stmt += f" REFERENCES {migration.references}"
            # Each value in stmt comes from a Migration in this code, never
            # from a caller or a request.
            await conn.exec_driver_sql(stmt)
