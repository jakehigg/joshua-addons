"""The full receipt flow: one date, one store, aliases, a fuzzy hit, a new
item, a same-day duplicate that collapses, and a second call that fills a
null UPC.
"""

from __future__ import annotations

from joshua_pantry import server
from joshua_pantry.models import Item, ItemAlias
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_DAY = "2026-08-15"


async def _seed(sessionmaker_: async_sessionmaker[AsyncSession]) -> None:
    """Pre-existing tracked items the receipt's alias and fuzzy lines should hit."""
    async with sessionmaker_() as session:
        milk = Item(name="milk", normalized="milk")
        yogurt = Item(name="greek yogurt", normalized="greek yogurt")
        banana = Item(name="banana", normalized="banana")
        session.add_all([milk, yogurt, banana])
        await session.flush()
        session.add_all(
            [
                ItemAlias(item_id=milk.id, alias="whole milk", normalized="whole milk"),
                ItemAlias(item_id=yogurt.id, alias="yogurt", normalized="yogurt"),
            ]
        )
        await session.commit()


async def test_receipt_scenario(bound_db, sessionmaker_) -> None:
    await _seed(sessionmaker_)

    lines = [
        server.PurchaseLine(name="whole milk", cost=3.50),  # alias hit -> milk
        server.PurchaseLine(name="yogurt", cost=4.00),  # alias hit -> greek yogurt
        server.PurchaseLine(name="bananas", cost=1.20),  # fuzzy hit -> banana
        server.PurchaseLine(name="kombucha", cost=3.99),  # new item
        server.PurchaseLine(name="eggs", cost=2.50, sku="1234"),  # new item, no upc yet
        server.PurchaseLine(name="bread", cost=2.75),
        server.PurchaseLine(name="butter", cost=4.25),
        server.PurchaseLine(name="cheese", cost=5.00),
        server.PurchaseLine(name="apples", cost=3.10),
        server.PurchaseLine(name="chicken", cost=6.50),
        server.PurchaseLine(name="rice", cost=2.20),
        server.PurchaseLine(name="eggs", cost=2.60),  # same-day duplicate of the eggs line
    ]
    assert len(lines) == 12

    result = await server.record_purchase(items=lines, purchased_at=_DAY, store="Aldi")

    assert result["purchased_at"] == _DAY
    assert len(result["items"]) == 12

    by_input_name = {row["name"]: row for row in result["items"]}

    assert by_input_name["whole milk"]["resolved_to"] == "Milk"
    assert by_input_name["whole milk"]["created"] is False

    assert by_input_name["yogurt"]["resolved_to"] == "Greek Yogurt"
    assert by_input_name["yogurt"]["created"] is False

    assert by_input_name["bananas"]["resolved_to"] == "Banana"
    assert by_input_name["bananas"]["created"] is False

    assert by_input_name["kombucha"]["created"] is True
    assert "Kombucha" in result["created_items"]

    # Every line took the top-level store; none set its own.
    assert all(row["store"] == "Aldi" for row in result["items"])

    # The two "eggs" lines collapse into one purchase record: the second is
    # a same-day merge, and its cost (2.60, last write wins) is what stuck.
    egg_lines = [row for row in result["items"] if row["name"] == "eggs"]
    assert len(egg_lines) == 2
    assert egg_lines[0]["merged_same_day"] is False
    assert egg_lines[1]["merged_same_day"] is True
    assert egg_lines[1]["cost"] == 2.60
    assert egg_lines[1]["upc"] is None  # no UPC recorded yet

    history = await server.get_item_history("Eggs")
    assert history["purchase_count"] == 1
    assert history["recent_purchases"][0]["unit_cost"] == 2.60
    assert history["recent_purchases"][0]["upc"] is None

    # A second, later call for the same date fills the null UPC without
    # touching the cost already on file.
    second = await server.record_purchase(
        items=[server.PurchaseLine(name="eggs", upc="012345678905")],
        purchased_at=_DAY,
    )
    assert second["items"][0]["merged_same_day"] is True
    assert second["items"][0]["created"] is False
    assert second["items"][0]["upc"] == "012345678905"
    assert second["items"][0]["cost"] == 2.60  # unchanged: the second call gave no cost

    history_after = await server.get_item_history("Eggs")
    assert history_after["purchase_count"] == 1  # still one record for that day
    assert history_after["recent_purchases"][0]["upc"] == "012345678905"
    assert history_after["recent_purchases"][0]["unit_cost"] == 2.60

    inventory = await server.get_inventory()
    names = {row["name"] for row in inventory}
    assert names == {
        "Milk",
        "Greek Yogurt",
        "Banana",
        "Kombucha",
        "Eggs",
        "Bread",
        "Butter",
        "Cheese",
        "Apples",
        "Chicken",
        "Rice",
    }
