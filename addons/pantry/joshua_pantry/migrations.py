"""Additive schema migrations for the pantry addon.

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
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Float, Integer, Text, inspect
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.types import TypeEngine

from .models import Base


@dataclass(frozen=True)
class Migration:
    """One additive column, applied when its table exists but the column does not.

    ``references``, when set, is a raw ``REFERENCES`` clause (e.g.
    ``"products(id) ON DELETE SET NULL"``) appended to the ``ADD COLUMN``
    statement. Both SQLite and Postgres accept an inline ``REFERENCES``
    clause on ``ALTER TABLE ... ADD COLUMN``, and every existing row gets the
    new column as NULL, which always satisfies a foreign key — so this is
    safe to add to a table that already has rows.

    ``constraint_name``, when set, names the foreign key with an explicit
    ``CONSTRAINT`` clause. Give it whenever the same column, on a table
    ``create_all`` builds fresh, gets a named constraint in ``models.py``
    (see ``Item.preferred_product_id``): Postgres otherwise auto-names the
    constraint from the inline ``REFERENCES`` clause (for example
    ``items_preferred_product_id_fkey``), so a database that took the
    additive path ends up with a different constraint name than one
    ``create_all`` built fresh — and a later ``DROP CONSTRAINT`` by the name
    in ``models.py`` (SQLAlchemy issues one for a ``use_alter`` foreign key
    on ``drop_all``) fails on the additive-path database, naming a
    constraint that database never had.
    """

    table: str
    column: str
    type_: TypeEngine
    references: str | None = None
    constraint_name: str | None = None


# Ordered oldest to newest. sku, upc, and quantity are the columns this addon
# adds to purchase_records beyond the ported base model (see models.py); they
# are the seed entries that exercise the mechanism against a database created
# before this addon's release added them. The preference columns (P2.3) add
# the preferred-product pointer and its confidence/source to items; the new
# ``products`` table itself needs no entry here — ``create_all`` (see
# ``run_migrations`` below) creates any missing table, including one that did
# not exist in a pre-P2.3 database.
ADDITIVE_MIGRATIONS: list[Migration] = [
    Migration("purchase_records", "sku", Text()),
    Migration("purchase_records", "upc", Text()),
    Migration("purchase_records", "quantity", Float()),
    Migration(
        "items",
        "preferred_product_id",
        Integer(),
        references="products(id) ON DELETE SET NULL",
        constraint_name="fk_items_preferred_product_id",
    ),
    Migration("items", "preference_confidence", Text()),
    Migration("items", "preference_source", Text()),
]


async def _existing_columns(conn: AsyncConnection, table: str) -> set[str]:
    def _inspect(sync_conn) -> set[str]:
        inspector = inspect(sync_conn)
        if not inspector.has_table(table):
            return set()
        return {col["name"] for col in inspector.get_columns(table)}

    return await conn.run_sync(_inspect)


async def run_migrations(engine: AsyncEngine) -> None:
    """Create any missing table, then apply any missing additive column."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with engine.begin() as conn:
        for migration in ADDITIVE_MIGRATIONS:
            existing = await _existing_columns(conn, migration.table)
            if not existing or migration.column in existing:
                continue
            compiled_type = migration.type_.compile(dialect=conn.dialect)
            stmt = f"ALTER TABLE {migration.table} ADD COLUMN {migration.column} {compiled_type}"
            if migration.references:
                if migration.constraint_name:
                    stmt += f" CONSTRAINT {migration.constraint_name}"
                stmt += f" REFERENCES {migration.references}"
            # migration.table/column/type_/references/constraint_name come
            # only from ADDITIVE_MIGRATIONS above, a fixed list in this file,
            # never
            # from a caller or request.
            await conn.exec_driver_sql(stmt)
