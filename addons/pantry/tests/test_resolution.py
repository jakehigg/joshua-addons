"""Name normalization and item resolution: exact, alias, fuzzy, empty."""

from __future__ import annotations

from joshua_pantry import resolution
from joshua_pantry.models import Item, ItemAlias
from sqlalchemy.ext.asyncio import AsyncSession


def test_normalize_collapses_punctuation_and_case() -> None:
    assert resolution.normalize("Non-Fat Greek Yogurt") == "non fat greek yogurt"
    assert resolution.normalize("non fat   greek yogurt") == "non fat greek yogurt"


async def test_resolve_exact_match(session: AsyncSession) -> None:
    item = Item(name="Bananas", normalized="bananas")
    session.add(item)
    await session.commit()

    found = await resolution.resolve_item(session, "Bananas")
    assert found is not None
    assert found.id == item.id


async def test_resolve_exact_match_includes_untracked(session: AsyncSession) -> None:
    """An untracked (hidden) item is still reused rather than duplicated."""
    item = Item(name="Bananas", normalized="bananas", is_tracked=False)
    session.add(item)
    await session.commit()

    found = await resolution.resolve_item(session, "Bananas")
    assert found is not None
    assert found.id == item.id


async def test_resolve_via_alias(session: AsyncSession) -> None:
    item = Item(name="Peanut Butter", normalized="peanut butter")
    session.add(item)
    await session.flush()
    session.add(ItemAlias(item_id=item.id, alias="pb", normalized="pb"))
    await session.commit()

    found = await resolution.resolve_item(session, "PB")
    assert found is not None
    assert found.id == item.id


async def test_resolve_fuzzy_positive_banana_bananas(session: AsyncSession) -> None:
    """Singular/plural of the same word is a conservative substring match."""
    item = Item(name="Banana", normalized="banana")
    session.add(item)
    await session.commit()

    found = await resolution.resolve_item(session, "Bananas")
    assert found is not None
    assert found.id == item.id


async def test_resolve_fuzzy_negative_milk_almond_milk(session: AsyncSession) -> None:
    """Distinct products never auto-conflate: milk and almond milk stay separate."""
    item = Item(name="Milk", normalized="milk")
    session.add(item)
    await session.commit()

    found = await resolution.resolve_item(session, "Almond Milk")
    assert found is None


async def test_resolve_fuzzy_negative_reverse_direction(session: AsyncSession) -> None:
    item = Item(name="Almond Milk", normalized="almond milk")
    session.add(item)
    await session.commit()

    found = await resolution.resolve_item(session, "Milk")
    assert found is None


async def test_resolve_empty_name_returns_none(session: AsyncSession) -> None:
    assert await resolution.resolve_item(session, "") is None
    assert await resolution.resolve_item(session, "   ") is None


async def test_resolve_no_fuzzy_skips_fuzzy_match(session: AsyncSession) -> None:
    item = Item(name="Banana", normalized="banana")
    session.add(item)
    await session.commit()

    found = await resolution.resolve_item(session, "Bananas", fuzzy=False)
    assert found is None


async def test_resolve_unknown_name_returns_none(session: AsyncSession) -> None:
    assert await resolution.resolve_item(session, "Unobtainium") is None


async def test_get_aliases_by_item_groups_and_sorts(session: AsyncSession) -> None:
    item = Item(name="Peanut Butter", normalized="peanut butter")
    session.add(item)
    await session.flush()
    session.add_all(
        [
            ItemAlias(item_id=item.id, alias="pb", normalized="pb"),
            ItemAlias(item_id=item.id, alias="peanut spread", normalized="peanut spread"),
        ]
    )
    await session.commit()

    grouped = await resolution.get_aliases_by_item(session)
    assert grouped[item.id] == ["pb", "peanut spread"]
