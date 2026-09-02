"""One purchase per item per day: creation, and same-day merge semantics."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from joshua_pantry import purchases
from joshua_pantry.models import Item, PurchaseRecord
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

_DAY = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


async def _make_item(session: AsyncSession, name: str = "Milk") -> Item:
    item = Item(name=name, normalized=name.lower())
    session.add(item)
    await session.flush()
    return item


async def test_upsert_creates_first_record(session: AsyncSession) -> None:
    item = await _make_item(session)

    rec, created = await purchases.upsert_purchase(session, item.id, _DAY, "manual")
    await session.commit()

    assert created is True
    assert rec.purchase_date == _DAY.date()
    assert rec.source == "manual"


async def test_upsert_second_call_same_day_merges_not_duplicates(session: AsyncSession) -> None:
    item = await _make_item(session)

    await purchases.upsert_purchase(session, item.id, _DAY, "automatic")
    await session.commit()

    later_same_day = _DAY.replace(hour=18)
    rec, created = await purchases.upsert_purchase(session, item.id, later_same_day, "manual")
    await session.commit()

    assert created is False

    result = await session.execute(select(PurchaseRecord).where(PurchaseRecord.item_id == item.id))
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].id == rec.id


async def test_upsert_different_day_creates_second_record(session: AsyncSession) -> None:
    item = await _make_item(session)

    await purchases.upsert_purchase(session, item.id, _DAY, "manual")
    await session.commit()

    next_day = datetime(2026, 1, 2, 9, 0, tzinfo=UTC)
    await purchases.upsert_purchase(session, item.id, next_day, "manual")
    await session.commit()

    result = await session.execute(select(PurchaseRecord).where(PurchaseRecord.item_id == item.id))
    rows = result.scalars().all()
    assert len(rows) == 2


async def test_upsert_unit_cost_and_store_are_last_write_wins(session: AsyncSession) -> None:
    item = await _make_item(session)

    await purchases.upsert_purchase(
        session, item.id, _DAY, "automatic", unit_cost=3.5, store="Aldi"
    )
    await session.commit()

    rec, _ = await purchases.upsert_purchase(
        session, item.id, _DAY, "manual", unit_cost=4.0, store="Kroger"
    )
    await session.commit()

    assert rec.unit_cost == 4.0
    assert rec.store == "Kroger"


async def test_upsert_sku_upc_quantity_fill_nulls_but_do_not_overwrite(
    session: AsyncSession,
) -> None:
    """A later same-day entry fills sku/upc/quantity left null, but a value
    already on file is never replaced by a second, different value."""
    item = await _make_item(session)

    rec, _ = await purchases.upsert_purchase(
        session, item.id, _DAY, "scan", sku="SKU-1", upc=None, quantity=1.0
    )
    await session.commit()
    assert rec.sku == "SKU-1"
    assert rec.upc is None
    assert rec.quantity == 1.0

    # A later call fills the still-null upc, but must not clobber the sku or
    # quantity already recorded, even though it supplies different values.
    rec, created = await purchases.upsert_purchase(
        session, item.id, _DAY.replace(hour=20), "scan", sku="SKU-2", upc="UPC-9", quantity=2.0
    )
    await session.commit()

    assert created is False
    assert rec.sku == "SKU-1"  # unchanged: was already set
    assert rec.upc == "UPC-9"  # filled: was null
    assert rec.quantity == 1.0  # unchanged: was already set


async def test_upsert_creates_with_sku_upc_quantity(session: AsyncSession) -> None:
    item = await _make_item(session)

    rec, created = await purchases.upsert_purchase(
        session, item.id, _DAY, "scan", sku="SKU-1", upc="UPC-1", quantity=2.5
    )
    await session.commit()

    assert created is True
    assert rec.sku == "SKU-1"
    assert rec.upc == "UPC-1"
    assert rec.quantity == 2.5


async def test_db_unique_constraint_is_the_backstop(session: AsyncSession) -> None:
    """A raw insert that bypasses upsert_purchase still hits the one-per-day constraint."""
    item = await _make_item(session)

    session.add(PurchaseRecord(item_id=item.id, purchased_at=_DAY, purchase_date=_DAY.date()))
    await session.commit()

    session.add(
        PurchaseRecord(
            item_id=item.id, purchased_at=_DAY.replace(hour=20), purchase_date=_DAY.date()
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
