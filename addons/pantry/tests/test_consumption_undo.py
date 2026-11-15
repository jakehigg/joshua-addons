"""Undo a consumption event, and take a quantity on ``consume_items``.

The first test replays the live case of 2026-10-28: one ``consume_items``
call, logged against a pudding, emptied three items that were bought two days
earlier and were still in the fridge. ``delete_consumption`` undoes it, and
every item reads in stock again with its purchase on file. The rest cover a
partial consumption, which leaves the item in stock, and the errors.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from conftest import mcp_session
from joshua_pantry import server
from mcp.server.mcpserver.exceptions import ToolError

# The purchase day is two days back in UTC, so a fresh purchase is recent
# enough to be in stock without a purchase cycle.
_BOUGHT = (datetime.now(UTC) - timedelta(days=2)).date().isoformat()

_PUDDING = ["chia seeds", "almond milk", "plain non-fat greek yogurt"]


async def test_delete_consumption_puts_every_item_of_one_call_back_in_stock(bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name=name, cost=3.0) for name in _PUDDING],
        purchased_at=_BOUGHT,
    )
    await server.consume_items(_PUDDING, note="chia pudding")

    rows = {r["name"]: r["status"] for r in await server.get_inventory()}
    assert set(rows.values()) == {"out_of_stock"}

    for name in _PUDDING:
        history = await server.get_item_history(name)
        [event] = history["recent_consumptions"]
        result = await server.delete_consumption(event["id"])
        assert result["deleted_consumption_id"] == event["id"]
        assert result["status"] == "in_stock"

    rows = {r["name"]: r for r in await server.get_inventory()}
    assert len(rows) == 3
    for row in rows.values():
        assert row["status"] == "in_stock"
        assert row["last_purchased_at"].startswith(_BOUGHT)

    # The purchase of each item is untouched, and no event remains.
    for name in _PUDDING:
        history = await server.get_item_history(name)
        assert history["purchase_count"] == 1
        assert history["last_cost"] == 3.0
        assert history["recent_consumptions"] == []


async def test_a_partial_consume_leaves_the_item_in_stock(bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Almond Milk", quantity=2.0)], purchased_at=_BOUGHT
    )

    await server.consume_items([server.ConsumptionLine(name="almond milk", quantity=1.0)])

    history = await server.get_item_history("almond milk")
    assert history["status"] == "in_stock"
    assert history["recent_consumptions"][0]["quantity"] == 1.0

    rows = {r["name"]: r["status"] for r in await server.get_inventory()}
    assert rows == {"Almond Milk": "in_stock"}

    # The rest of the stock empties the item.
    await server.consume_items([server.ConsumptionLine(name="almond milk", quantity=1.0)])

    history = await server.get_item_history("almond milk")
    assert history["status"] == "out_of_stock"
    assert len(history["recent_consumptions"]) == 2


async def test_a_quantity_on_a_purchase_with_no_quantity_counts_one_unit(bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Chia Seeds")], purchased_at=_BOUGHT
    )

    await server.consume_items([server.ConsumptionLine(name="chia seeds", quantity=0.25)])
    assert (await server.get_item_history("chia seeds"))["status"] == "in_stock"

    await server.consume_items([server.ConsumptionLine(name="chia seeds", quantity=0.75)])
    assert (await server.get_item_history("chia seeds"))["status"] == "out_of_stock"


async def test_a_name_with_no_quantity_still_uses_the_item_up(bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Greek Yogurt", quantity=4.0)], purchased_at=_BOUGHT
    )

    await server.consume_items(["greek yogurt"])

    history = await server.get_item_history("greek yogurt")
    assert history["status"] == "out_of_stock"
    assert history["recent_consumptions"][0]["quantity"] is None


async def test_delete_consumption_unknown_id_raises(bound_db) -> None:
    with pytest.raises(ToolError, match="No consumption event with id 999"):
        await server.delete_consumption(999)


async def test_consume_items_rejects_a_quantity_of_zero_or_less(bound_db) -> None:
    with pytest.raises(ToolError, match="quantity of 'Kale' must be more than 0"):
        await server.consume_items([server.ConsumptionLine(name="Kale", quantity=0)])


async def test_consume_items_takes_a_quantity_over_mcp(app_factory) -> None:
    app = app_factory(None)
    async with mcp_session(app) as session:
        await session.call_tool(
            "record_purchase",
            {"items": [{"name": "Chia Seeds", "quantity": 2}], "purchased_at": _BOUGHT},
        )
        consumed = await session.call_tool(
            "consume_items",
            {"items": [{"name": "chia seeds", "quantity": 1}, "almond milk"], "note": "pudding"},
        )
        assert consumed.is_error is not True
        assert consumed.structured_content["consumed"] == ["Chia Seeds", "Almond Milk"]

        history = await session.call_tool("get_item_history", {"item_name": "chia seeds"})
        assert history.structured_content["status"] == "in_stock"
        event_id = history.structured_content["recent_consumptions"][0]["id"]

        undone = await session.call_tool("delete_consumption", {"consumption_id": event_id})
        assert undone.is_error is not True
        assert undone.structured_content["item"] == "Chia Seeds"
        assert undone.structured_content["status"] == "in_stock"
