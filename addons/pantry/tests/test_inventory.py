"""Cycle computation, status transitions, and the DB-backed inventory path."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from joshua_pantry import inventory
from joshua_pantry.models import ConsumptionEvent, Inventory, Item, PurchaseRecord
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _purchases(*days_from_base: float) -> list[PurchaseRecord]:
    return [PurchaseRecord(purchased_at=_BASE + timedelta(days=d)) for d in days_from_base]


# --- _compute_cycle: v1 (all history, min_intervals=1) and v2 (window=12, min_intervals=3) ---


def test_compute_cycle_v1_needs_at_least_two_purchases() -> None:
    avg, depletion = inventory._compute_cycle(_purchases(0), window=None, min_intervals=1)
    assert (avg, depletion) == (None, None)


def test_compute_cycle_v1_uses_all_history() -> None:
    purchases = _purchases(0, 3, 9)  # intervals: 3, 6 -> median 4.5
    avg, depletion = inventory._compute_cycle(purchases, window=None, min_intervals=1)
    assert avg == 4.5
    assert depletion == purchases[-1].purchased_at + timedelta(days=4.5)


def test_compute_cycle_v2_requires_min_purchase_count() -> None:
    # v2 needs 3 intervals (4 purchases); 2 intervals is not enough.
    purchases = _purchases(0, 3, 6)
    avg, depletion = inventory._compute_cycle(purchases, **inventory.ALGO_VARIANTS["v2"])
    assert (avg, depletion) == (None, None)


def test_compute_cycle_v2_computes_with_enough_purchases() -> None:
    # 4 purchases -> 3 intervals of 2 days each -> median 2.
    purchases = _purchases(0, 2, 4, 6)
    avg, depletion = inventory._compute_cycle(purchases, **inventory.ALGO_VARIANTS["v2"])
    assert avg == 2
    assert depletion == purchases[-1].purchased_at + timedelta(days=2)


def test_compute_cycle_v2_windows_to_last_12_intervals() -> None:
    # 14 purchases -> 13 intervals. The first interval is 30 days (an outlier);
    # the rest are 1 day. A window of 12 drops that outlier, so the median
    # comes out at 1, not skewed by the old gap.
    days = [0, 30] + [30 + i for i in range(1, 13)]
    purchases = _purchases(*days)
    avg, _ = inventory._compute_cycle(purchases, **inventory.ALGO_VARIANTS["v2"])
    assert avg == 1


def test_compute_cycle_median_math_with_odd_interval_out() -> None:
    # intervals: 1, 1, 10 -> median 1, not skewed by the outlier.
    purchases = _purchases(0, 1, 2, 12)
    avg, _ = inventory._compute_cycle(purchases, window=None, min_intervals=1)
    assert avg == 1


# --- get_item_status: unknown -> in_stock -> likely_depleted, and out_of_stock ---


def test_status_unknown_with_no_inventory() -> None:
    assert inventory.get_item_status(None) == "unknown"


def test_status_in_stock_recent_purchase_no_cycle_yet() -> None:
    inv = Inventory(item_id=1, last_purchased_at=datetime.now(UTC) - timedelta(days=1))
    assert inventory.get_item_status(inv) == "in_stock"


def test_status_unknown_when_stale_and_no_cycle() -> None:
    inv = Inventory(item_id=1, last_purchased_at=datetime.now(UTC) - timedelta(days=40))
    assert inventory.get_item_status(inv) == "unknown"


def test_status_in_stock_before_estimated_depletion() -> None:
    now = datetime.now(UTC)
    inv = Inventory(
        item_id=1,
        last_purchased_at=now - timedelta(days=1),
        avg_cycle_days=7,
        estimated_depletion=now + timedelta(days=6),
    )
    assert inventory.get_item_status(inv) == "in_stock"


def test_status_likely_depleted_after_estimated_depletion() -> None:
    now = datetime.now(UTC)
    inv = Inventory(
        item_id=1,
        last_purchased_at=now - timedelta(days=10),
        avg_cycle_days=2,
        estimated_depletion=now - timedelta(days=8),
    )
    assert inventory.get_item_status(inv) == "likely_depleted"


def test_status_out_of_stock_when_consumed_after_purchase() -> None:
    now = datetime.now(UTC)
    inv = Inventory(
        item_id=1,
        last_purchased_at=now - timedelta(days=5),
        avg_cycle_days=7,
        estimated_depletion=now + timedelta(days=2),
    )
    # Even though the cycle math says in_stock, a consumption logged after the
    # last purchase overrides it.
    assert inventory.get_item_status(inv, last_consumed_at=now - timedelta(days=1)) == (
        "out_of_stock"
    )


def test_status_not_out_of_stock_when_consumed_before_purchase() -> None:
    now = datetime.now(UTC)
    inv = Inventory(item_id=1, last_purchased_at=now - timedelta(days=1))
    assert inventory.get_item_status(inv, last_consumed_at=now - timedelta(days=5)) == "in_stock"


# --- DB-backed: recalculate_inventory, get_all_inventory_status, get_purchase_analytics ---


async def test_recalculate_inventory_creates_row_from_purchases(session: AsyncSession) -> None:
    item = Item(name="Milk", normalized="milk")
    session.add(item)
    await session.flush()

    for d in (0, 2, 4, 6):
        session.add(
            PurchaseRecord(
                item_id=item.id,
                purchased_at=_BASE + timedelta(days=d),
                purchase_date=(_BASE + timedelta(days=d)).date(),
            )
        )
    await session.commit()

    await inventory.recalculate_inventory(session, item.id)

    result = await session.execute(select(Inventory).where(Inventory.item_id == item.id))
    inv = result.scalar_one()
    assert inv.avg_cycle_days == 2
    assert inv.algo_version == inventory.ALGO_VERSION


async def test_recalculate_inventory_updates_an_existing_row(session: AsyncSession) -> None:
    """A second call, after a new purchase, updates the row rather than inserting one."""
    item = Item(name="Milk", normalized="milk")
    session.add(item)
    await session.flush()

    session.add(PurchaseRecord(item_id=item.id, purchased_at=_BASE, purchase_date=_BASE.date()))
    await session.commit()
    await inventory.recalculate_inventory(session, item.id)

    later = _BASE + timedelta(days=3)
    session.add(PurchaseRecord(item_id=item.id, purchased_at=later, purchase_date=later.date()))
    await session.commit()
    await inventory.recalculate_inventory(session, item.id)

    result = await session.execute(select(Inventory).where(Inventory.item_id == item.id))
    rows = result.scalars().all()
    assert len(rows) == 1  # updated in place, not duplicated
    assert rows[0].last_purchased_at == later


async def test_recalculate_inventory_pulls_depletion_forward_on_consumption(
    session: AsyncSession,
) -> None:
    item = Item(name="Eggs", normalized="eggs")
    session.add(item)
    await session.flush()

    purchased_at = _BASE
    session.add(
        PurchaseRecord(
            item_id=item.id, purchased_at=purchased_at, purchase_date=purchased_at.date()
        )
    )
    await session.commit()

    consumed_at = purchased_at + timedelta(days=1)
    session.add(ConsumptionEvent(item_id=item.id, occurred_at=consumed_at))
    await session.commit()

    await inventory.recalculate_inventory(session, item.id)

    result = await session.execute(select(Inventory).where(Inventory.item_id == item.id))
    inv = result.scalar_one()
    assert inv.estimated_depletion == consumed_at


async def test_recalculate_inventory_noop_with_no_purchases(session: AsyncSession) -> None:
    item = Item(name="Ghost", normalized="ghost")
    session.add(item)
    await session.commit()

    await inventory.recalculate_inventory(session, item.id)

    result = await session.execute(select(Inventory).where(Inventory.item_id == item.id))
    assert result.scalar_one_or_none() is None


async def test_get_all_inventory_status_reports_untouched_item_as_unknown(
    session: AsyncSession,
) -> None:
    item = Item(name="Flour", normalized="flour")
    session.add(item)
    await session.commit()

    rows = await inventory.get_all_inventory_status(session)
    assert len(rows) == 1
    assert rows[0]["name"] == "Flour"
    assert rows[0]["status"] == "unknown"
    assert rows[0]["preferred_store"] is None


async def test_get_all_inventory_status_filters_by_status(session: AsyncSession) -> None:
    tracked = Item(name="Rice", normalized="rice")
    untracked = Item(name="Salt", normalized="salt", is_tracked=False)
    session.add_all([tracked, untracked])
    await session.commit()

    rows = await inventory.get_all_inventory_status(session, status_filter="unknown")
    names = {r["name"] for r in rows}
    assert names == {"Rice"}


async def test_get_all_inventory_status_filter_skips_non_matching_rows(
    session: AsyncSession,
) -> None:
    """A status_filter that doesn't match every tracked item exercises the skip branch."""
    unknown_item = Item(name="Flour", normalized="flour")
    in_stock_item = Item(name="Milk", normalized="milk")
    session.add_all([unknown_item, in_stock_item])
    await session.flush()

    purchased_at = datetime.now(UTC) - timedelta(days=1)
    session.add(
        PurchaseRecord(
            item_id=in_stock_item.id, purchased_at=purchased_at, purchase_date=purchased_at.date()
        )
    )
    await session.commit()
    await inventory.recalculate_inventory(session, in_stock_item.id)

    rows = await inventory.get_all_inventory_status(session, status_filter="unknown")
    names = {r["name"] for r in rows}
    assert names == {"Flour"}


async def test_get_all_inventory_status_sorts_action_statuses_by_frequency(
    session: AsyncSession,
) -> None:
    """likely_depleted/out_of_stock items sort by shortest cycle first."""
    slow = Item(name="Detergent", normalized="detergent")
    fast = Item(name="Milk", normalized="milk")
    session.add_all([slow, fast])
    await session.flush()

    # Both end up likely_depleted, slow with a 20-day cycle, fast with a 2-day
    # cycle -- fast must sort first.
    for item, spacing in ((slow, 20), (fast, 2)):
        for d in (0, spacing, spacing * 2, spacing * 3):
            purchased_at = _BASE + timedelta(days=d)
            session.add(
                PurchaseRecord(
                    item_id=item.id,
                    purchased_at=purchased_at,
                    purchase_date=purchased_at.date(),
                )
            )
        await session.commit()
        await inventory.recalculate_inventory(session, item.id)

    rows = await inventory.get_all_inventory_status(session)
    names_in_order = [r["name"] for r in rows if r["status"] == "likely_depleted"]
    assert names_in_order == ["Milk", "Detergent"]


async def test_get_purchase_analytics_computes_cost_and_intervals(session: AsyncSession) -> None:
    item = Item(name="Coffee", normalized="coffee")
    session.add(item)
    await session.flush()

    for d, cost in ((0, 10.0), (5, 12.0), (15, None)):
        purchased_at = _BASE + timedelta(days=d)
        session.add(
            PurchaseRecord(
                item_id=item.id,
                purchased_at=purchased_at,
                purchase_date=purchased_at.date(),
                unit_cost=cost,
            )
        )
    await session.commit()

    rows = await inventory.get_purchase_analytics(session)
    assert len(rows) == 1
    row = rows[0]
    assert row["purchase_count"] == 3
    assert row["shortest_interval_days"] == 5.0
    assert row["longest_interval_days"] == 10.0
    assert row["median_interval_days"] == 7.5
    assert row["avg_cost"] == 11.0
    # The most recent purchase (day 15) has no cost recorded, so last_cost
    # falls back to the last purchase that did have one (day 5, $12).
    assert row["last_cost"] == 12.0


async def test_simulate_inventory_compares_variants_without_touching_live_table(
    session: AsyncSession,
) -> None:
    item = Item(name="Coffee", normalized="coffee")
    untracked = Item(name="Hidden", normalized="hidden", is_tracked=False)
    session.add_all([item, untracked])
    await session.flush()

    for d in (0, 2, 4, 6):
        purchased_at = _BASE + timedelta(days=d)
        session.add(
            PurchaseRecord(
                item_id=item.id, purchased_at=purchased_at, purchase_date=purchased_at.date()
            )
        )
    await session.commit()

    rows = await inventory.simulate_inventory(session)

    assert [r["name"] for r in rows] == ["Coffee"]  # untracked items are excluded
    row = rows[0]
    assert row["purchase_count"] == 4
    assert set(row["variants"]) == {"v1", "v2"}
    assert row["variants"]["v2"]["avg_cycle_days"] == 2
    assert row["variants"]["v2"]["status"] in {"in_stock", "likely_depleted"}

    # The live inventory table is untouched by a simulation.
    result = await session.execute(select(Inventory).where(Inventory.item_id == item.id))
    assert result.scalar_one_or_none() is None
