"""Model shape: the poller tables and mag_id are gone; new purchase columns exist."""

from __future__ import annotations

import pytest
from joshua_pantry import models
from joshua_pantry.models import Category, Item, Product, PurchaseRecord
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
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


def test_item_has_preference_columns() -> None:
    columns = Item.__table__.columns
    assert "preferred_product_id" in columns
    assert columns["preferred_product_id"].nullable
    assert "preference_confidence" in columns
    assert columns["preference_confidence"].nullable
    assert "preference_source" in columns
    assert columns["preference_source"].nullable


def test_product_has_every_expected_column() -> None:
    columns = Product.__table__.columns
    for name in (
        "item_id",
        "upc",
        "sku",
        "description",
        "store",
        "size",
        "unit",
        "extra",
        "last_purchased_at",
        "created_at",
        "updated_at",
    ):
        assert name in columns


async def test_product_upc_unique_when_set(session: AsyncSession) -> None:
    item = Item(name="Spaghetti Sauce", normalized="spaghetti sauce")
    session.add(item)
    await session.flush()

    session.add(Product(item_id=item.id, upc="111"))
    await session.commit()

    session.add(Product(item_id=item.id, upc="111"))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_product_upc_null_allows_any_number_of_rows(session: AsyncSession) -> None:
    """A bare UNIQUE constraint treats NULL as distinct from NULL on both
    SQLite and Postgres, so two products with no known UPC never collide."""
    item = Item(name="Spaghetti Sauce", normalized="spaghetti sauce")
    session.add(item)
    await session.flush()

    session.add(Product(item_id=item.id, upc=None, sku="A"))
    session.add(Product(item_id=item.id, upc=None, sku="B"))
    await session.commit()  # must not raise

    result = await session.execute(select(Product).where(Product.item_id == item.id))
    assert len(result.scalars().all()) == 2


async def test_product_deleted_with_item_cascade(session: AsyncSession) -> None:
    item = Item(name="Spaghetti Sauce", normalized="spaghetti sauce")
    session.add(item)
    await session.flush()
    session.add(Product(item_id=item.id, upc="222"))
    await session.commit()

    await session.delete(item)
    await session.commit()

    result = await session.execute(select(Product))
    assert result.scalars().all() == []


async def test_deleting_preferred_product_clears_the_pointer(session: AsyncSession) -> None:
    item = Item(name="Spaghetti Sauce", normalized="spaghetti sauce")
    session.add(item)
    await session.flush()
    product = Product(item_id=item.id, upc="333")
    session.add(product)
    await session.flush()

    item.preferred_product_id = product.id
    item.preference_confidence = "auto"
    item.preference_source = "manual"
    await session.commit()

    await session.delete(product)
    await session.commit()
    await session.refresh(item)

    assert item.preferred_product_id is None


async def test_fresh_engine_reports_expected_tables(session: AsyncSession) -> None:
    bind = await session.connection()
    table_names = await bind.run_sync(lambda conn: set(inspect(conn).get_table_names()))
    assert table_names == {
        "categories",
        "items",
        "item_aliases",
        "purchase_records",
        "products",
        "inventory",
        "consumption_events",
    }
