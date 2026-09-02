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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import PurchaseRecord


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
