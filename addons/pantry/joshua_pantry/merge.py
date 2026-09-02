"""Item renaming, alias creation, and merging for the REST API.

Ported from the joshua-pantry backend
(``pantry/backend/app/services/merge.py``). This addon has no poller-only
tables (``Event``, ``SnapshotItem``) and no reminder id (``Item.mag_id``) to
carry across a merge, so this version only moves purchases, consumption
events, aliases, and inventory state.
"""

from __future__ import annotations

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import inventory as inv_service
from .models import ConsumptionEvent, Inventory, Item, ItemAlias, PurchaseRecord
from .resolution import normalize


class AliasConflictError(Exception):
    """The alias already points to a different item."""

    def __init__(self, alias: str, owner: Item) -> None:
        self.alias = alias
        self.owner = owner
        super().__init__(f"Alias '{alias}' already points to '{owner.name}'")


class DuplicateItemError(Exception):
    """The alias name is itself an existing item (caller must merge explicitly)."""

    def __init__(self, item: Item) -> None:
        self.item = item
        super().__init__(f"'{item.name}' already exists as item {item.id}")


async def merge_items(session: AsyncSession, source: Item, target: Item) -> dict:
    """Merge ``source`` into ``target``, preserving combined history. Commits."""
    if source.id == target.id:
        raise ValueError("Cannot merge an item into itself")

    # Move purchase history, resolving same-day conflicts inline. The
    # uq_purchase_records_item_date constraint forbids two purchases for one
    # item on a single date, so a bulk reassignment blows up when source and
    # target were both bought on the same day. Keep the target's record for a
    # conflicting date and drop the source's duplicate.
    target_dates = set(
        (
            await session.execute(
                select(PurchaseRecord.purchase_date).where(PurchaseRecord.item_id == target.id)
            )
        )
        .scalars()
        .all()
    )
    source_records = (
        (
            await session.execute(
                select(PurchaseRecord)
                .where(PurchaseRecord.item_id == source.id)
                .order_by(PurchaseRecord.purchased_at)
            )
        )
        .scalars()
        .all()
    )
    moved_purchases = 0
    for record in source_records:
        if record.purchase_date in target_dates:
            await session.delete(record)
        else:
            record.item_id = target.id
            target_dates.add(record.purchase_date)
            moved_purchases += 1
    await session.flush()

    await session.execute(
        update(ConsumptionEvent)
        .where(ConsumptionEvent.item_id == source.id)
        .values(item_id=target.id)
    )
    await session.execute(delete(Inventory).where(Inventory.item_id == source.id))

    # Source's aliases follow it, and its own name becomes an alias.
    await session.execute(
        update(ItemAlias).where(ItemAlias.item_id == source.id).values(item_id=target.id)
    )
    alias_added = None
    if source.normalized != target.normalized:
        existing = await session.execute(
            select(ItemAlias).where(ItemAlias.normalized == source.normalized)
        )
        if existing.scalars().first() is None:
            session.add(
                ItemAlias(
                    item_id=target.id,
                    alias=source.name,
                    normalized=source.normalized,
                    source="merge",
                )
            )
            alias_added = source.name

    if source.first_seen and source.first_seen < target.first_seen:
        target.first_seen = source.first_seen
    for attr in ("last_seen", "last_purchased_at"):
        src_val = getattr(source, attr)
        tgt_val = getattr(target, attr)
        if src_val and (tgt_val is None or src_val > tgt_val):
            setattr(target, attr, src_val)
    target.is_tracked = source.is_tracked or target.is_tracked
    if target.category_id is None and source.category_id is not None:
        target.category_id = source.category_id

    await session.flush()
    await session.execute(delete(Item).where(Item.id == source.id))
    await session.commit()
    await inv_service.recalculate_inventory(session, target.id)

    return {
        "target_id": target.id,
        "target_name": target.name,
        "alias_added": alias_added,
        "moved_purchases": moved_purchases,
    }


async def rename_item(session: AsyncSession, item: Item, new_name: str) -> Item:
    """Rename ``item``'s canonical name; the old name becomes an alias. Commits."""
    norm = normalize(new_name)
    if not norm:
        raise ValueError("New name is empty")
    if norm == item.normalized:
        return item

    # Conflict: another tracked item already has this name.
    result = await session.execute(select(Item).where(Item.normalized == norm, Item.id != item.id))
    other_item = result.scalars().first()
    if other_item is not None:
        raise ValueError(
            f"'{new_name.strip()}' is already a tracked item ('{other_item.name.title()}'). "
            f"Use merge to combine them."
        )

    # Conflict: the new name is an alias of a different item.
    result = await session.execute(
        select(ItemAlias).where(ItemAlias.normalized == norm, ItemAlias.item_id != item.id)
    )
    other_alias = result.scalars().first()
    if other_alias is not None:
        owner = await session.get(Item, other_alias.item_id)
        raise AliasConflictError(new_name, owner)

    # The new name is already an alias of this item: it is promoted to
    # canonical, so drop the now-redundant alias row.
    result = await session.execute(
        select(ItemAlias).where(ItemAlias.normalized == norm, ItemAlias.item_id == item.id)
    )
    self_alias = result.scalars().first()
    if self_alias is not None:
        await session.delete(self_alias)

    # The old canonical name becomes an alias, unless already aliased.
    result = await session.execute(
        select(ItemAlias).where(
            ItemAlias.normalized == item.normalized, ItemAlias.item_id == item.id
        )
    )
    if result.scalars().first() is None:
        session.add(
            ItemAlias(item_id=item.id, alias=item.name, normalized=item.normalized, source="rename")
        )

    item.name = new_name.strip().lower()
    item.normalized = norm
    await session.commit()
    return item


async def add_alias(
    session: AsyncSession,
    target: Item,
    alias_name: str,
    *,
    source: str = "manual",
    merge_duplicates: bool = True,
) -> dict:
    """Add an alias to ``target``. Commits.

    When the alias name is itself an existing item, merges it into
    ``target`` if ``merge_duplicates`` is set, otherwise raises
    ``DuplicateItemError``. Raises ``AliasConflictError`` when the alias
    already belongs to a different item.
    """
    norm = normalize(alias_name)
    if not norm:
        raise ValueError("Alias name is empty")

    base = {
        "target_id": target.id,
        "target_name": target.name,
        "alias_added": None,
        "moved_purchases": 0,
        "merged": False,
    }
    if norm == target.normalized:
        return base

    result = await session.execute(select(Item).where(Item.normalized == norm))
    duplicate = result.scalars().first()
    if duplicate is not None:
        if not merge_duplicates:
            raise DuplicateItemError(duplicate)
        merged = await merge_items(session, duplicate, target)
        return {**base, **merged, "merged": True}

    result = await session.execute(select(ItemAlias).where(ItemAlias.normalized == norm))
    existing = result.scalars().first()
    if existing is not None:
        if existing.item_id == target.id:
            return base
        owner = await session.get(Item, existing.item_id)
        raise AliasConflictError(alias_name, owner)

    display = alias_name.strip().lower()
    session.add(ItemAlias(item_id=target.id, alias=display, normalized=norm, source=source))
    await session.commit()
    return {**base, "alias_added": display}
