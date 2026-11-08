"""The ``check_items`` tool: every item for a food, and one verdict (issue #14)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from joshua_pantry import server
from joshua_pantry.models import Item
from joshua_pantry.resolution import match_kind
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import update

_RECENT = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()


def _by_item(result: dict) -> dict[str, dict]:
    return {m["item"]: m for m in result["matches"]}


async def _buy(name: str, day: str = _RECENT) -> None:
    await server.record_purchase(items=[server.PurchaseLine(name=name)], purchased_at=day)


async def test_one_variant_in_stock_and_one_depleted_gives_in_stock(bound_db) -> None:
    await _buy("organic baby spinach")
    await _buy("fresh spinach", day="2026-01-01")
    await server.consume_items(["fresh spinach"])
    await _buy("frozen chopped spinach")
    await _buy("kale")

    (result,) = (await server.check_items(["spinach"]))["results"]

    assert result["name"] == "spinach"
    assert result["verdict"] == "in_stock"
    matches = _by_item(result)
    assert set(matches) == {"Organic Baby Spinach", "Fresh Spinach", "Frozen Chopped Spinach"}
    assert matches["Fresh Spinach"]["status"] == "out_of_stock"
    assert matches["Organic Baby Spinach"]["status"] == "in_stock"
    assert matches["Organic Baby Spinach"]["matched_via"] == "words"
    assert matches["Organic Baby Spinach"]["last_purchased_at"].startswith(_RECENT)
    # In-stock matches come first.
    assert result["matches"][-1]["item"] == "Fresh Spinach"


async def test_multi_word_query_needs_every_word(bound_db) -> None:
    await _buy("little yellow potatoes")
    await _buy("yellow onions")
    await _buy("potatoes")

    (result,) = (await server.check_items(["yellow potatoes"]))["results"]

    assert set(_by_item(result)) == {"Little Yellow Potatoes"}
    assert result["verdict"] == "in_stock"


async def test_match_through_an_alias(bound_db) -> None:
    await _buy("granola bars")
    await server.add_alias("granola bars", "trail mix bars")

    (result,) = (await server.check_items(["Trail-Mix Bars"]))["results"]

    assert _by_item(result)["Granola Bars"]["matched_via"] == "alias"
    assert result["verdict"] == "in_stock"


async def test_exact_name_match(bound_db) -> None:
    await _buy("mushrooms")

    (result,) = (await server.check_items(["Mushrooms"]))["results"]

    assert _by_item(result)["Mushrooms"]["matched_via"] == "name"


async def test_name_with_no_match(bound_db) -> None:
    await _buy("mushrooms")

    results = (await server.check_items(["mushrooms", "saffron"]))["results"]

    assert [r["verdict"] for r in results] == ["in_stock", "no_match"]
    assert results[1]["matches"] == []


async def test_no_match_in_stock_gives_the_best_other_status(bound_db) -> None:
    # Four purchases two days apart, long ago: the cycle says likely depleted.
    for day in ("2026-01-01", "2026-01-03", "2026-01-05", "2026-01-07"):
        await _buy("oat milk", day=day)
    await _buy("chocolate oat milk", day="2026-01-01")
    await server.consume_items(["chocolate oat milk"])

    (result,) = (await server.check_items(["oat milk"]))["results"]

    statuses = {m["item"]: m["status"] for m in result["matches"]}
    assert statuses == {"Oat Milk": "likely_depleted", "Chocolate Oat Milk": "out_of_stock"}
    assert result["verdict"] == "likely_depleted"


async def test_untracked_item_is_not_a_match(bound_db) -> None:
    await _buy("mushrooms")
    async with bound_db() as session:
        await session.execute(update(Item).values(is_tracked=False))
        await session.commit()

    (result,) = (await server.check_items(["mushrooms"]))["results"]

    assert result["verdict"] == "no_match"


async def test_empty_names_raises(bound_db) -> None:
    with pytest.raises(ToolError, match="names is empty"):
        await server.check_items([])


def test_match_kind_rules() -> None:
    assert match_kind("spinach", "Baby Spinach", []) == "words"
    assert match_kind("baby spinach", "Spinach", []) is None
    assert match_kind("spin", "Spinach", []) is None
    assert match_kind("bars", "Granola", ["Trail Mix Bars"]) == "words"
    assert match_kind("  ", "Spinach", []) is None
