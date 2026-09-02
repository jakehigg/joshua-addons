"""Direct, data-layer tests for ``joshua_pantry.merge``: merge, rename, and alias conflicts."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from joshua_pantry import merge
from joshua_pantry.models import Item, ItemAlias, PurchaseRecord
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_DAY = datetime(2026, 1, 1, tzinfo=UTC)


async def _make_item(session: AsyncSession, name: str) -> Item:
    item = Item(name=name.lower(), normalized=name.lower())
    session.add(item)
    await session.flush()
    return item


async def test_merge_items_moves_purchases_and_adds_alias(session: AsyncSession) -> None:
    source = await _make_item(session, "Bread")
    target = await _make_item(session, "Milk")
    session.add(PurchaseRecord(item_id=source.id, purchased_at=_DAY, purchase_date=_DAY.date()))
    await session.commit()

    result = await merge.merge_items(session, source, target)

    assert result["target_name"] == "milk"
    assert result["alias_added"] == "bread"
    assert result["moved_purchases"] == 1

    aliases = (
        (await session.execute(select(ItemAlias).where(ItemAlias.item_id == target.id)))
        .scalars()
        .all()
    )
    assert len(aliases) == 1


async def test_merge_items_same_id_raises_value_error(session: AsyncSession) -> None:
    item = await _make_item(session, "Bread")
    await session.commit()
    with pytest.raises(ValueError, match="itself"):
        await merge.merge_items(session, item, item)


async def test_rename_item_conflict_with_existing_item_raises(session: AsyncSession) -> None:
    await _make_item(session, "Bread")
    other = await _make_item(session, "Milk")
    await session.commit()

    with pytest.raises(ValueError, match="already a tracked item"):
        await merge.rename_item(session, other, "Bread")


async def test_rename_item_promotes_matching_alias(session: AsyncSession) -> None:
    item = await _make_item(session, "Bread")
    session.add(ItemAlias(item_id=item.id, alias="sourdough", normalized="sourdough"))
    await session.commit()

    renamed = await merge.rename_item(session, item, "Sourdough")

    assert renamed.name == "sourdough"
    # The old canonical name is now the alias; the promoted alias row is gone.
    aliases = (await session.execute(select(ItemAlias))).scalars().all()
    assert [row.alias for row in aliases] == ["bread"]


async def test_add_alias_of_existing_item_raises_when_merge_disabled(session: AsyncSession) -> None:
    target = await _make_item(session, "Bread")
    await _make_item(session, "Milk")
    await session.commit()

    with pytest.raises(merge.DuplicateItemError):
        await merge.add_alias(session, target, "Milk", merge_duplicates=False)


async def test_add_alias_of_existing_item_merges_when_enabled(session: AsyncSession) -> None:
    target = await _make_item(session, "Bread")
    await _make_item(session, "Milk")
    await session.commit()

    result = await merge.add_alias(session, target, "Milk", merge_duplicates=True)

    assert result["merged"] is True
    assert result["target_id"] == target.id


async def test_add_alias_conflicting_with_another_items_alias_raises(session: AsyncSession) -> None:
    owner = await _make_item(session, "Bread")
    target = await _make_item(session, "Milk")
    session.add(ItemAlias(item_id=owner.id, alias="toast", normalized="toast"))
    await session.commit()

    with pytest.raises(merge.AliasConflictError):
        await merge.add_alias(session, target, "Toast")
