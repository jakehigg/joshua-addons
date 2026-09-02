"""Product find-or-create, the upc-collision refusal, and the compact dict shape."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from joshua_pantry.models import Item, Product
from joshua_pantry.products import (
    find_or_create_preference_product,
    find_or_create_product,
    product_dict,
)
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_DAY = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


async def _make_item(session: AsyncSession, name: str = "Spaghetti Sauce") -> Item:
    item = Item(name=name, normalized=name.lower())
    session.add(item)
    await session.flush()
    return item


async def test_no_upc_or_sku_store_returns_none(session: AsyncSession) -> None:
    item = await _make_item(session)
    product = await find_or_create_product(session, item, when=_DAY)
    assert product is None


async def test_creates_a_new_product_from_a_upc(session: AsyncSession) -> None:
    item = await _make_item(session)

    product = await find_or_create_product(
        session, item, when=_DAY, upc="111", description="Rao's Marinara", size="24 oz"
    )
    await session.commit()

    assert product is not None
    assert product.item_id == item.id
    assert product.upc == "111"
    assert product.description == "Rao's Marinara"
    assert product.last_purchased_at == _DAY


async def test_second_sighting_by_upc_updates_not_duplicates(session: AsyncSession) -> None:
    item = await _make_item(session)

    first = await find_or_create_product(session, item, when=_DAY, upc="111")
    await session.commit()

    later = _DAY.replace(day=15)
    second = await find_or_create_product(session, item, when=later, upc="111")
    await session.commit()

    assert second.id == first.id
    assert second.last_purchased_at == later

    result = await session.execute(select(Product).where(Product.item_id == item.id))
    assert len(result.scalars().all()) == 1


async def test_second_sighting_fills_blank_fields_but_does_not_overwrite(
    session: AsyncSession,
) -> None:
    item = await _make_item(session)

    await find_or_create_product(session, item, when=_DAY, upc="111", size="24 oz")
    await session.commit()

    product = await find_or_create_product(
        session, item, when=_DAY, upc="111", size="16 oz", description="Rao's Marinara"
    )
    await session.commit()

    assert product.size == "24 oz"  # unchanged: was already set
    assert product.description == "Rao's Marinara"  # filled: was blank


async def test_sku_and_store_match_without_a_upc(session: AsyncSession) -> None:
    item = await _make_item(session)

    first = await find_or_create_product(session, item, when=_DAY, sku="SKU-1", store="Aldi")
    await session.commit()

    second = await find_or_create_product(session, item, when=_DAY, sku="SKU-1", store="Aldi")
    await session.commit()

    assert second.id == first.id


async def test_sku_alone_without_store_does_not_match(session: AsyncSession) -> None:
    """sku alone is not enough to match — the same sku can repeat across stores."""
    item = await _make_item(session)

    product = await find_or_create_product(session, item, when=_DAY, sku="SKU-1")
    assert product is None


async def test_upc_under_a_different_item_raises_naming_both(session: AsyncSession) -> None:
    sauce = await _make_item(session, "Spaghetti Sauce")
    await find_or_create_product(session, sauce, when=_DAY, upc="999")
    await session.commit()

    salsa = await _make_item(session, "Salsa")
    with pytest.raises(ToolError, match="Spaghetti Sauce") as excinfo:
        await find_or_create_product(session, salsa, when=_DAY, upc="999")
    assert "Salsa" in str(excinfo.value)


async def test_preference_product_matches_by_sku_alone(session: AsyncSession) -> None:
    """Unlike find_or_create_product, a bare sku (no store) is enough for a preference."""
    item = await _make_item(session)

    first = await find_or_create_preference_product(session, item, sku="SKU-1")
    await session.commit()

    second = await find_or_create_preference_product(session, item, sku="SKU-1")
    await session.commit()

    assert second.id == first.id


async def test_preference_product_creates_from_upc_only(session: AsyncSession) -> None:
    item = await _make_item(session)

    product = await find_or_create_preference_product(
        session, item, upc="111", description="Rao's Marinara", size="24 oz"
    )
    await session.commit()

    assert product.upc == "111"
    assert product.description == "Rao's Marinara"
    assert product.sku is None


async def test_preference_product_sku_match_respects_a_given_store(session: AsyncSession) -> None:
    item = await _make_item(session)

    aldi = await find_or_create_preference_product(session, item, sku="SKU-1", store="Aldi")
    await session.commit()
    kroger = await find_or_create_preference_product(session, item, sku="SKU-1", store="Kroger")
    await session.commit()

    assert aldi.id != kroger.id


async def test_preference_product_second_sighting_by_upc_fills_sku_store_and_description(
    session: AsyncSession,
) -> None:
    item = await _make_item(session)

    await find_or_create_preference_product(session, item, upc="111")
    await session.commit()

    product = await find_or_create_preference_product(
        session, item, upc="111", sku="SKUX", store="Aldi", description="Rao's"
    )
    await session.commit()

    assert product.sku == "SKUX"  # filled: was blank
    assert product.store == "Aldi"  # filled: was blank
    assert product.description == "Rao's"  # filled: was blank


async def test_preference_product_second_sighting_by_sku_fills_upc_size_and_unit(
    session: AsyncSession,
) -> None:
    item = await _make_item(session)

    await find_or_create_preference_product(session, item, sku="SKU-1")
    await session.commit()

    product = await find_or_create_preference_product(
        session, item, sku="SKU-1", upc="222", size="24 oz", unit="jar"
    )
    await session.commit()

    assert product.upc == "222"  # filled: was blank
    assert product.size == "24 oz"  # filled: was blank
    assert product.unit == "jar"  # filled: was blank


async def test_preference_product_upc_under_a_different_item_raises(
    session: AsyncSession,
) -> None:
    sauce = await _make_item(session, "Spaghetti Sauce")
    await find_or_create_preference_product(session, sauce, upc="999")
    await session.commit()

    salsa = await _make_item(session, "Salsa")
    with pytest.raises(ToolError, match="Spaghetti Sauce") as excinfo:
        await find_or_create_preference_product(session, salsa, upc="999")
    assert "Salsa" in str(excinfo.value)


def test_product_dict_of_none_is_none() -> None:
    assert product_dict(None) is None


def test_product_dict_compact_shape() -> None:
    product = Product(
        item_id=1,
        upc="111",
        sku="SKU-1",
        description="Rao's Marinara",
        store="Aldi",
        size="24 oz",
        unit="jar",
        last_purchased_at=_DAY,
    )
    assert product_dict(product) == {
        "upc": "111",
        "sku": "SKU-1",
        "description": "Rao's Marinara",
        "size": "24 oz",
        "unit": "jar",
        "store": "Aldi",
        "last_purchased_at": _DAY.isoformat(),
    }
