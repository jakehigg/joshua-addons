"""Model shape: the poller tables and mag_id are gone; new purchase columns exist."""

from __future__ import annotations

from joshua_pantry import models
from joshua_pantry.models import Category, Item, PurchaseRecord
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession


def test_poller_only_tables_are_not_in_metadata() -> None:
    table_names = set(models.Base.metadata.tables)
    assert "snapshots" not in table_names
    assert "snapshot_items" not in table_names
    assert "events" not in table_names


def test_item_has_no_mag_id_column() -> None:
    assert "mag_id" not in Item.__table__.columns


def test_purchase_record_has_sku_upc_quantity_columns() -> None:
    columns = PurchaseRecord.__table__.columns
    assert "sku" in columns
    assert columns["sku"].nullable
    assert "upc" in columns
    assert columns["upc"].nullable
    assert "quantity" in columns
    assert columns["quantity"].nullable


def test_purchase_record_keeps_one_per_item_per_day_constraint() -> None:
    names = {c.name for c in PurchaseRecord.__table__.constraints}
    assert "uq_purchase_records_item_date" in names


async def test_category_set_null_on_delete(session: AsyncSession) -> None:
    category = Category(name="Dairy", normalized="dairy")
    session.add(category)
    await session.flush()

    item = Item(name="Milk", normalized="milk", category_id=category.id)
    session.add(item)
    await session.commit()

    await session.delete(category)
    await session.commit()
    await session.refresh(item)

    assert item.category_id is None


async def test_fresh_engine_reports_expected_tables(session: AsyncSession) -> None:
    bind = await session.connection()
    table_names = await bind.run_sync(lambda conn: set(inspect(conn).get_table_names()))
    assert table_names == {
        "categories",
        "items",
        "item_aliases",
        "purchase_records",
        "inventory",
        "consumption_events",
    }
