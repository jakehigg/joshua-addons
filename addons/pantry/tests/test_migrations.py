"""Schema creation, its idempotence, and the additive-column migration path."""

from __future__ import annotations

from joshua_pantry.migrations import ADDITIVE_MIGRATIONS, run_migrations
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine


async def _columns(engine: AsyncEngine, table: str) -> set[str]:
    async with engine.begin() as conn:

        def _inspect(sync_conn) -> set[str]:
            return {col["name"] for col in inspect(sync_conn).get_columns(table)}

        return await conn.run_sync(_inspect)


async def test_fresh_start_creates_every_column(raw_engine: AsyncEngine) -> None:
    await run_migrations(raw_engine)

    columns = await _columns(raw_engine, "purchase_records")
    assert {"sku", "upc", "quantity", "unit_cost", "store"} <= columns


async def test_fresh_start_is_idempotent(raw_engine: AsyncEngine) -> None:
    await run_migrations(raw_engine)
    await run_migrations(raw_engine)  # must not raise on a second run

    columns = await _columns(raw_engine, "purchase_records")
    assert "quantity" in columns


async def test_additive_migration_applies_to_a_pre_migration_schema(
    raw_engine: AsyncEngine,
) -> None:
    """A table created before sku/upc/quantity existed still picks them up."""
    async with raw_engine.begin() as conn:
        # A stand-in for the pre-migration table shape: everything except the
        # three columns this addon adds (models.py + migrations.py together).
        await conn.exec_driver_sql(
            """
            CREATE TABLE purchase_records (
                id INTEGER PRIMARY KEY,
                item_id INTEGER NOT NULL,
                purchased_at TIMESTAMP NOT NULL,
                purchase_date DATE NOT NULL,
                source TEXT NOT NULL DEFAULT 'automatic',
                unit_cost FLOAT,
                store TEXT,
                CONSTRAINT uq_purchase_records_item_date UNIQUE (item_id, purchase_date)
            )
            """
        )

    before = await _columns(raw_engine, "purchase_records")
    assert "quantity" not in before
    assert "sku" not in before
    assert "upc" not in before

    await run_migrations(raw_engine)

    after = await _columns(raw_engine, "purchase_records")
    assert {"sku", "upc", "quantity"} <= after

    # The new columns are usable: nullable, so an existing insert style
    # (omitting them) still works.
    async with raw_engine.begin() as conn:
        await conn.exec_driver_sql(
            "INSERT INTO purchase_records "
            "(id, item_id, purchased_at, purchase_date, source) "
            "VALUES (1, 1, '2026-01-01 00:00:00', '2026-01-01', 'manual')"
        )


def test_additive_migrations_list_has_a_real_entry() -> None:
    # The mechanism must be exercised by at least one real column, not left
    # as dead code with an empty list.
    assert any(
        m.table == "purchase_records" and m.column == "quantity" for m in ADDITIVE_MIGRATIONS
    )
