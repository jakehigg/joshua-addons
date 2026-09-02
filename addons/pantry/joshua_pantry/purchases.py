"""Purchase recording: at most one purchase per item per day.

Ported from the joshua-pantry backend
(``pantry/backend/app/services/purchases.py``): ``upsert_purchase`` merges
same-day entries into one row instead of racing the one-per-day constraint.
Extended here to carry ``sku``/``upc``/``quantity``: unlike ``unit_cost`` and
``store``, which a later call always overwrites when given, these three only
fill a null left by an earlier call — a second scan of the same item on the
same day should not let a missing or different barcode silently clobber the
one already recorded.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import inventory as inv_service
from .models import Item, PurchaseRecord


async def upsert_purchase(
    session: AsyncSession,
    item_id: int,
    when: datetime,
    source: str,
    unit_cost: float | None = None,
    store: str | None = None,
    sku: str | None = None,
    upc: str | None = None,
    quantity: float | None = None,
) -> tuple[PurchaseRecord, bool]:
    """Record a purchase, at most one per item per day.

    If a purchase already exists for this item on ``when``'s UTC date, merge
    into it instead of inserting a duplicate: ``unit_cost``/``store`` are
    overwritten whenever a caller supplies a value (last write wins, matching
    the source service), while ``sku``/``upc``/``quantity`` only fill the
    column when it is still null (first write wins), so a later, less-specific
    entry for the same day cannot erase a barcode or quantity already on file.
    This makes repeated recording of the same purchase idempotent and
    order-independent. The DB unique constraint on (item_id, purchase_date) is
    the hard backstop.

    ``when`` must be timezone-aware UTC so its ``.date()`` is the UTC day.
    Returns ``(record, created)``.
    """
    purchase_date = when.date()

    result = await session.execute(
        select(PurchaseRecord).where(
            PurchaseRecord.item_id == item_id,
            PurchaseRecord.purchase_date == purchase_date,
        )
    )
    rec = result.scalar_one_or_none()

    if rec is None:
        rec = PurchaseRecord(
            item_id=item_id,
            purchased_at=when,
            purchase_date=purchase_date,
            source=source,
            unit_cost=unit_cost,
            store=store,
            sku=sku,
            upc=upc,
            quantity=quantity,
        )
        session.add(rec)
        return rec, True

    if unit_cost is not None:
        rec.unit_cost = unit_cost
    if store is not None:
        rec.store = store
    if sku is not None and rec.sku is None:
        rec.sku = sku
    if upc is not None and rec.upc is None:
        rec.upc = upc
    if quantity is not None and rec.quantity is None:
        rec.quantity = quantity
    return rec, False


def _serialize(rec: PurchaseRecord, item_name: str) -> dict:
    """Shape one purchase row for the REST API: item name title-cased, dates as ISO strings."""
    return {
        "id": rec.id,
        "item_id": rec.item_id,
        "item_name": item_name.title(),
        "purchased_at": rec.purchased_at.isoformat(),
        "purchase_date": rec.purchase_date.isoformat(),
        "source": rec.source,
        "unit_cost": rec.unit_cost,
        "store": rec.store,
    }


async def list_purchases(session: AsyncSession, limit: int, offset: int) -> tuple[list[dict], int]:
    """Every purchase record across all items, newest first.

    Returns ``(rows, total)``: ``rows`` is one page (``limit``/``offset``) of
    serialized purchases sorted by ``purchased_at`` descending, and ``total``
    is the full unpaginated count -- the admin view grows unbounded, so it
    pages rather than loading everything at once. The item id is a stable
    tiebreaker so paging can't skip or repeat rows that share an instant.
    """
    total = await session.scalar(select(func.count()).select_from(PurchaseRecord))

    result = await session.execute(
        select(PurchaseRecord, Item.name)
        .join(Item, PurchaseRecord.item_id == Item.id)
        .order_by(PurchaseRecord.purchased_at.desc(), PurchaseRecord.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = [_serialize(rec, name) for rec, name in result.all()]
    return rows, total or 0


async def update_purchase(session: AsyncSession, purchase_id: int, fields: dict) -> dict | None:
    """Edit cost/store on a single existing purchase, in place.

    ``fields`` carries only the keys the caller actually sent (``unit_cost``
    / ``store``); a key present with ``None`` clears that column -- the same
    semantics as the ``set_purchase_cost`` / ``set_purchase_store`` MCP
    tools. Neither column feeds the frequency/depletion algorithm (that keys
    off ``purchased_at`` alone), so no inventory recalculation runs. Returns
    the updated row, or ``None`` when no purchase has that id.
    """
    rec = await session.get(PurchaseRecord, purchase_id)
    if rec is None:
        return None

    if "unit_cost" in fields:
        rec.unit_cost = fields["unit_cost"]
    if "store" in fields:
        rec.store = fields["store"]
    await session.commit()

    item_name = await session.scalar(select(Item.name).where(Item.id == rec.item_id))
    return _serialize(rec, item_name)


async def delete_purchase(session: AsyncSession, purchase_id: int) -> bool:
    """Delete one purchase record and recompute the owning item's inventory.

    Only this row and the item's derived inventory stats are touched -- the
    item, its aliases, category, and other purchases are left alone. Returns
    ``False`` when no purchase has that id.
    """
    rec = await session.get(PurchaseRecord, purchase_id)
    if rec is None:
        return False

    item_id = rec.item_id
    await session.delete(rec)
    await session.commit()
    await inv_service.recalculate_inventory(session, item_id)
    return True
