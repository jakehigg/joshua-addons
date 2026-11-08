"""The ``merge_items`` MCP tool, and what a merge keeps on a same-day collision (issue #13)."""

from __future__ import annotations

import pytest
from joshua_pantry import server
from joshua_pantry.models import Item, Product, PurchaseRecord
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import select

_DAY = "2026-08-01"
_LATER = "2026-08-05"


async def _purchases_of(sessionmaker_, name: str) -> dict[str, PurchaseRecord]:
    async with sessionmaker_() as session:
        item = (await session.execute(select(Item).where(Item.normalized == name))).scalar_one()
        rows = (
            (await session.execute(select(PurchaseRecord).where(PurchaseRecord.item_id == item.id)))
            .scalars()
            .all()
        )
    return {r.purchase_date.isoformat(): r for r in rows}


async def test_merge_keeps_every_field_of_two_purchases_on_one_date(bound_db) -> None:
    """Both items have a purchase on one date. The merged purchase keeps each field."""
    await server.record_purchase(
        items=[server.PurchaseLine(name="little potatoes", cost=3.49, store="store a")],
        purchased_at=_DAY,
    )
    await server.record_purchase(
        items=[
            server.PurchaseLine(
                name="yellow potatoes", sku="111", upc="000222333", quantity=2, store="store b"
            )
        ],
        purchased_at=_DAY,
    )
    await server.record_purchase(
        items=[server.PurchaseLine(name="yellow potatoes", cost=3.99)], purchased_at=_LATER
    )

    result = await server.merge_items(source="yellow potatoes", target="little potatoes")

    assert result["source"] == "Yellow Potatoes"
    assert result["target"] == "Little Potatoes"
    assert result["moved_purchases"] == 1
    assert result["combined_purchases"] == 1
    assert result["aliases_added"] == ["Yellow Potatoes"]
    assert result["purchase_conflicts"] == [
        {"purchase_date": _DAY, "field": "store", "kept": "store a", "dropped": "store b"}
    ]

    purchases = await _purchases_of(bound_db, "little potatoes")
    assert set(purchases) == {_DAY, _LATER}
    same_day = purchases[_DAY]
    assert same_day.unit_cost == 3.49
    assert same_day.store == "store a"
    assert same_day.sku == "111"
    assert same_day.upc == "000222333"
    assert same_day.quantity == 2
    assert purchases[_LATER].unit_cost == 3.99

    # The source's product moved with it.
    async with bound_db() as session:
        product = (
            await session.execute(select(Product).where(Product.upc == "000222333"))
        ).scalar_one()
        target = (
            await session.execute(select(Item).where(Item.normalized == "little potatoes"))
        ).scalar_one()
        gone = (
            await session.execute(select(Item).where(Item.normalized == "yellow potatoes"))
        ).scalar_one_or_none()
    assert product.item_id == target.id
    assert gone is None

    # The source name now resolves to the target.
    history = await server.get_item_history("yellow potatoes")
    assert history["item"] == "Little Potatoes"
    assert history["matched_via"] == "alias"


async def test_merge_reports_the_source_aliases_it_moves(bound_db) -> None:
    await server.record_purchase(items=[server.PurchaseLine(name="spinach")], purchased_at=_DAY)
    await server.record_purchase(
        items=[server.PurchaseLine(name="baby spinach")], purchased_at=_LATER
    )
    await server.add_alias("baby spinach", "organic baby spinach")

    result = await server.merge_items(source="baby spinach", target="spinach")

    assert result["aliases_added"] == ["Baby Spinach", "Organic Baby Spinach"]
    assert result["moved_purchases"] == 1
    assert result["purchase_conflicts"] == []


async def test_merge_refuses_an_item_into_itself(bound_db) -> None:
    await server.record_purchase(items=[server.PurchaseLine(name="spinach")], purchased_at=_DAY)
    await server.add_alias("spinach", "fresh spinach")

    with pytest.raises(ToolError, match="same item"):
        await server.merge_items(source="fresh spinach", target="spinach")


async def test_merge_unknown_item_raises(bound_db) -> None:
    await server.record_purchase(items=[server.PurchaseLine(name="spinach")], purchased_at=_DAY)

    with pytest.raises(ToolError, match="Nothing Here"):
        await server.merge_items(source="Nothing Here", target="spinach")
    with pytest.raises(ToolError, match="Nothing Here"):
        await server.merge_items(source="spinach", target="Nothing Here")


async def test_add_alias_of_a_tracked_item_points_at_merge_items(bound_db) -> None:
    await server.record_purchase(items=[server.PurchaseLine(name="spinach")], purchased_at=_DAY)
    await server.record_purchase(
        items=[server.PurchaseLine(name="baby spinach")], purchased_at=_DAY
    )

    with pytest.raises(ToolError, match="merge_items"):
        await server.add_alias("spinach", "baby spinach")
