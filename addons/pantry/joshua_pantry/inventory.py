"""Purchase-frequency inventory estimation.

Ported from the joshua-pantry backend
(``pantry/backend/app/services/inventory.py``). This addon has no poller:
purchases arrive through ``purchases.upsert_purchase`` from receipts, barcode
scans, and manual entries, and ``recalculate_inventory`` derives status from
``purchase_records`` the same way regardless of source.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from statistics import mean, median

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Category, ConsumptionEvent, Inventory, Item, Product, PurchaseRecord
from .products import product_dict
from .resolution import get_aliases_by_item

# Bump this when the live algorithm changes so inventory rows are stamped.
ALGO_VERSION = "v2"


def _display(name: str) -> str:
    """Title-case a lowercase DB name for user-facing output."""
    return name.title()


# Algorithm variant definitions — used by both the live path and simulate.
ALGO_VARIANTS: dict[str, dict] = {
    "v1": {"window": None, "min_intervals": 1},  # all history, min 2 purchases
    "v2": {"window": 12, "min_intervals": 3},  # last 12 intervals, min 4 purchases
}


def _compute_cycle(
    purchases: list[PurchaseRecord],
    window: int | None,
    min_intervals: int,
) -> tuple[float | None, datetime | None]:
    """Return (avg_cycle_days, estimated_depletion) for a purchase list."""
    if len(purchases) < 2:
        return None, None

    intervals = [
        (purchases[i + 1].purchased_at - purchases[i].purchased_at).total_seconds() / 86400
        for i in range(len(purchases) - 1)
    ]

    windowed = intervals[-window:] if window else intervals
    if len(windowed) < min_intervals:
        return None, None

    avg = median(windowed)
    depletion = purchases[-1].purchased_at + timedelta(days=avg)
    return avg, depletion


def get_item_status(inv: Inventory | None, last_consumed_at: datetime | None = None) -> str:
    now = datetime.now(UTC)
    last_purchase = inv.last_purchased_at if inv else None

    if last_consumed_at and (last_purchase is None or last_consumed_at > last_purchase):
        return "out_of_stock"

    if last_purchase is None:
        return "unknown"
    if inv.estimated_depletion is None:
        days_since = (now - last_purchase).total_seconds() / 86400
        return "in_stock" if days_since < 30 else "unknown"
    return "in_stock" if now < inv.estimated_depletion else "likely_depleted"


async def recalculate_inventory(session: AsyncSession, item_id: int) -> None:
    result = await session.execute(
        select(PurchaseRecord)
        .where(PurchaseRecord.item_id == item_id)
        .order_by(PurchaseRecord.purchased_at)
    )
    purchases = result.scalars().all()

    if not purchases:
        return

    now = datetime.now(UTC)
    last_purchased_at = purchases[-1].purchased_at
    params = ALGO_VARIANTS[ALGO_VERSION]
    avg_cycle_days, estimated_depletion = _compute_cycle(purchases, **params)

    # If manually consumed after last purchase, pull depletion forward.
    result = await session.execute(
        select(ConsumptionEvent)
        .where(
            ConsumptionEvent.item_id == item_id,
            ConsumptionEvent.occurred_at > last_purchased_at,
        )
        .order_by(ConsumptionEvent.occurred_at.desc())
        .limit(1)
    )
    consumed = result.scalar_one_or_none()
    if consumed:
        estimated_depletion = consumed.occurred_at

    result = await session.execute(select(Inventory).where(Inventory.item_id == item_id))
    inv = result.scalar_one_or_none()

    if inv:
        inv.last_purchased_at = last_purchased_at
        inv.avg_cycle_days = avg_cycle_days
        inv.estimated_depletion = estimated_depletion
        inv.algo_version = ALGO_VERSION
        inv.updated_at = now
    else:
        session.add(
            Inventory(
                item_id=item_id,
                last_purchased_at=last_purchased_at,
                avg_cycle_days=avg_cycle_days,
                estimated_depletion=estimated_depletion,
                algo_version=ALGO_VERSION,
                updated_at=now,
            )
        )

    await session.commit()


async def get_all_inventory_status(
    session: AsyncSession,
    status_filter: str | None = None,
) -> list[dict]:
    latest_consumption = (
        select(
            ConsumptionEvent.item_id,
            func.max(ConsumptionEvent.occurred_at).label("last_consumed_at"),
        )
        .group_by(ConsumptionEvent.item_id)
        .subquery()
    )

    result = await session.execute(
        select(Item, Inventory, latest_consumption.c.last_consumed_at, Category.name, Product)
        .outerjoin(Inventory, Item.id == Inventory.item_id)
        .outerjoin(latest_consumption, Item.id == latest_consumption.c.item_id)
        .outerjoin(Category, Item.category_id == Category.id)
        .outerjoin(Product, Item.preferred_product_id == Product.id)
        .where(Item.is_tracked.is_(True))
        .order_by(Item.name)
    )
    rows = result.all()
    aliases_map = await get_aliases_by_item(session)

    out = []
    for item, inv, last_consumed_at, category_name, preferred_product in rows:
        status = get_item_status(inv, last_consumed_at)
        if status_filter and status != status_filter:
            continue
        lpa = item.last_purchased_at or (inv.last_purchased_at if inv else None)
        out.append(
            {
                "item_id": item.id,
                "name": _display(item.name),
                "aliases": [_display(a) for a in aliases_map.get(item.id, [])],
                "category": _display(category_name) if category_name else None,
                "preferred_store": item.preferred_store,
                "preferred_product": product_dict(preferred_product),
                "status": status,
                "last_purchased_at": lpa.isoformat() if lpa else None,
                "avg_cycle_days": inv.avg_cycle_days if inv else None,
                "estimated_depletion": (
                    inv.estimated_depletion.isoformat() if inv and inv.estimated_depletion else None
                ),
                "updated_at": inv.updated_at.isoformat() if inv and inv.updated_at else None,
                "algo_version": inv.algo_version if inv else None,
            }
        )

    # Sort depleted/out_of_stock by purchase frequency (shortest cycle = most frequent first).
    # in_stock and unknown stay alphabetical from the SQL ORDER BY.
    def _freq_key(i: dict) -> tuple:
        acd = i["avg_cycle_days"]
        return (acd is None, acd or 0)

    action_statuses = {"out_of_stock", "likely_depleted"}
    stable = [i for i in out if i["status"] not in action_statuses]
    by_freq = sorted([i for i in out if i["status"] in action_statuses], key=_freq_key)
    return stable + by_freq


async def get_purchase_analytics(session: AsyncSession) -> list[dict]:
    """
    Per-item purchase statistics for the analytics view: how often and how
    regularly each tracked item has been bought.

    Intervals are the gaps (in days) between consecutive purchases. The live
    depletion algorithm uses the median of a recent window of these intervals
    (surfaced as ``cycle_days``); shortest/median/longest are computed over the
    full history so trends are visible as data accumulates.
    """
    result = await session.execute(
        select(Item, Inventory, Category.name)
        .outerjoin(Inventory, Item.id == Inventory.item_id)
        .outerjoin(Category, Item.category_id == Category.id)
        .where(Item.is_tracked.is_(True))
        .order_by(Item.name)
    )
    rows = result.all()
    aliases_map = await get_aliases_by_item(session)

    out = []
    for item, inv, category_name in rows:
        pr_result = await session.execute(
            select(PurchaseRecord)
            .where(PurchaseRecord.item_id == item.id)
            .order_by(PurchaseRecord.purchased_at)
        )
        purchases = pr_result.scalars().all()

        intervals = [
            (purchases[i + 1].purchased_at - purchases[i].purchased_at).total_seconds() / 86400
            for i in range(len(purchases) - 1)
        ]

        # purchases are ordered oldest-first; costs in the same order
        costs = [p.unit_cost for p in purchases if p.unit_cost is not None]

        out.append(
            {
                "item_id": item.id,
                "name": _display(item.name),
                "aliases": [_display(a) for a in aliases_map.get(item.id, [])],
                "category": _display(category_name) if category_name else None,
                "purchase_count": len(purchases),
                "first_purchased_at": (
                    purchases[0].purchased_at.isoformat() if purchases else None
                ),
                "last_purchased_at": (
                    purchases[-1].purchased_at.isoformat() if purchases else None
                ),
                "shortest_interval_days": round(min(intervals), 1) if intervals else None,
                "median_interval_days": round(median(intervals), 1) if intervals else None,
                "longest_interval_days": round(max(intervals), 1) if intervals else None,
                "cycle_days": round(inv.avg_cycle_days, 1) if inv and inv.avg_cycle_days else None,
                "avg_cost": round(mean(costs), 2) if costs else None,
                "last_cost": costs[-1] if costs else None,
            }
        )

    # Most-purchased first; ties broken alphabetically (SQL order is preserved).
    out.sort(key=lambda i: -i["purchase_count"])
    return out


async def simulate_inventory(session: AsyncSession) -> list[dict]:
    """
    Run every ALGO_VARIANTS against the real purchase_records and return
    a side-by-side comparison without touching the live inventory table.
    """
    result = await session.execute(
        select(Item).where(Item.is_tracked.is_(True)).order_by(Item.name)
    )
    items = result.scalars().all()

    now = datetime.now(UTC)
    out = []

    for item in items:
        pr_result = await session.execute(
            select(PurchaseRecord)
            .where(PurchaseRecord.item_id == item.id)
            .order_by(PurchaseRecord.purchased_at)
        )
        purchases = pr_result.scalars().all()

        variants: dict[str, dict] = {}
        for name, params in ALGO_VARIANTS.items():
            avg, depletion = _compute_cycle(purchases, **params)
            last_purchase = purchases[-1].purchased_at if purchases else None

            if last_purchase is None:
                status = "unknown"
            elif depletion is None:
                days_since = (now - last_purchase).total_seconds() / 86400
                status = "in_stock" if days_since < 30 else "unknown"
            else:
                status = "in_stock" if now < depletion else "likely_depleted"

            variants[name] = {
                "avg_cycle_days": round(avg, 1) if avg else None,
                "estimated_depletion": depletion.isoformat() if depletion else None,
                "status": status,
                "intervals_used": (
                    min(len(purchases) - 1, params["window"] or 9999) if len(purchases) >= 2 else 0
                ),
            }

        out.append(
            {
                "item_id": item.id,
                "name": _display(item.name),
                "purchase_count": len(purchases),
                "variants": variants,
            }
        )

    return out
