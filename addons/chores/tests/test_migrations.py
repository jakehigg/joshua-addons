"""Schema creation, its idempotence, and the additive-column path."""

from __future__ import annotations

from datetime import datetime

from joshua_chores.migrations import ADDITIVE_MIGRATIONS, Migration, run_migrations
from joshua_chores.models import Base, UTCDateTime
from sqlalchemy import Integer, Text, inspect
from sqlalchemy.ext.asyncio import AsyncEngine


async def _columns(engine: AsyncEngine, table: str) -> set[str]:
    async with engine.begin() as conn:
        return await conn.run_sync(
            lambda sync_conn: {col["name"] for col in inspect(sync_conn).get_columns(table)}
        )


async def _table_names(engine: AsyncEngine) -> set[str]:
    async with engine.begin() as conn:
        return await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))


async def test_fresh_start_creates_every_model_table(raw_engine: AsyncEngine) -> None:
    await run_migrations(raw_engine)
    assert set(Base.metadata.tables) <= await _table_names(raw_engine)


async def test_fresh_start_is_idempotent(raw_engine: AsyncEngine) -> None:
    await run_migrations(raw_engine)
    await run_migrations(raw_engine)


def test_additive_migrations_is_a_list() -> None:
    assert isinstance(ADDITIVE_MIGRATIONS, list)


async def test_additive_migration_adds_a_missing_column(raw_engine: AsyncEngine) -> None:
    async with raw_engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE parents (id INTEGER PRIMARY KEY)")
        await conn.exec_driver_sql("CREATE TABLE children (id INTEGER PRIMARY KEY)")
        await conn.exec_driver_sql("INSERT INTO parents (id) VALUES (1)")
        await conn.exec_driver_sql("INSERT INTO children (id) VALUES (1)")

    migrations = [
        Migration("children", "label", Text()),
        Migration(
            "children",
            "parent_id",
            Integer(),
            references="parents(id) ON DELETE SET NULL",
            constraint_name="fk_children_parent_id",
        ),
        Migration("absent_table", "label", Text()),
    ]
    await run_migrations(raw_engine, migrations)
    await run_migrations(raw_engine, migrations)

    assert {"id", "label", "parent_id"} <= await _columns(raw_engine, "children")
    assert "absent_table" not in await _table_names(raw_engine)
    async with raw_engine.begin() as conn:
        await conn.exec_driver_sql("UPDATE children SET parent_id = 1, label = 'x' WHERE id = 1")
        await conn.exec_driver_sql("DROP TABLE children")
        await conn.exec_driver_sql("DROP TABLE parents")


def test_utc_datetime_stamps_utc_on_a_naive_value() -> None:
    column_type = UTCDateTime()
    naive = datetime(2026, 1, 1, 12, 0)
    bound = column_type.process_bind_param(naive, None)
    read = column_type.process_result_value(naive, None)
    assert bound is not None and bound.utcoffset() is not None
    assert read is not None and read.utcoffset() is not None
    assert column_type.process_bind_param(None, None) is None
