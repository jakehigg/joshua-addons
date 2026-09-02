"""Per-tool happy paths, called directly against a bound test database."""

from __future__ import annotations

import pytest
from joshua_pantry import server
from mcp.server.mcpserver.exceptions import ToolError

_DAY = "2026-08-01"


async def test_record_purchase_creates_a_new_item(bound_db) -> None:
    result = await server.record_purchase(
        items=[server.PurchaseLine(name="Kombucha", cost=3.99)],
        purchased_at=_DAY,
    )
    assert result["items"][0]["created"] is True
    assert result["items"][0]["resolved_to"] == "Kombucha"
    assert result["created_items"] == ["Kombucha"]


async def test_get_inventory_lists_recorded_items(bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Eggs", cost=2.5)], purchased_at=_DAY
    )
    rows = await server.get_inventory()
    assert [r["name"] for r in rows] == ["Eggs"]


async def test_get_inventory_status_filter(bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Eggs", cost=2.5)], purchased_at=_DAY
    )
    rows = await server.get_inventory(status_filter="out_of_stock")
    assert rows == []


async def test_get_item_history_reports_matched_via_and_barcode_fields(bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Eggs", cost=2.5, sku="123", upc="000111222")],
        purchased_at=_DAY,
    )
    history = await server.get_item_history("Eggs")
    assert history["matched_via"] == "name"
    assert history["purchase_count"] == 1
    line = history["recent_purchases"][0]
    assert line["sku"] == "123"
    assert line["upc"] == "000111222"


async def test_get_item_history_unknown_item_raises(bound_db) -> None:
    with pytest.raises(ToolError, match="Nonexistent"):
        await server.get_item_history("Nonexistent")


async def test_get_item_cost_reports_average_and_missing(bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Eggs", cost=2.0)], purchased_at="2026-08-01"
    )
    await server.record_purchase(
        items=[server.PurchaseLine(name="Eggs", cost=4.0)], purchased_at="2026-08-02"
    )
    result = await server.get_item_cost(["Eggs", "Nothing"])
    by_name = {row["name"]: row for row in result["items"]}
    assert by_name["Eggs"]["found"] is True
    assert by_name["Eggs"]["avg_cost"] == 3.0
    assert by_name["Nothing"]["found"] is False


async def test_consume_items_marks_out_of_stock_status(bound_db) -> None:
    result = await server.consume_items(["Eggs"], note="omelette")
    assert result["consumed"] == ["Eggs"]
    assert result["created"] == ["Eggs"]


async def test_set_preferred_store_sets_and_clears(bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Eggs", cost=2.5)], purchased_at=_DAY
    )
    set_result = await server.set_preferred_store("Eggs", "Aldi")
    assert set_result == {"item": "Eggs", "preferred_store": "Aldi", "cleared": False}

    cleared_result = await server.set_preferred_store("Eggs")
    assert cleared_result == {"item": "Eggs", "preferred_store": None, "cleared": True}


async def test_set_preferred_store_unknown_item_raises(bound_db) -> None:
    with pytest.raises(ToolError, match="Nonexistent"):
        await server.set_preferred_store("Nonexistent", "Aldi")


async def test_set_purchase_cost_backfills_a_price(bound_db) -> None:
    await server.record_purchase(items=[server.PurchaseLine(name="Eggs")], purchased_at=_DAY)
    result = await server.set_purchase_cost("Eggs", _DAY, 3.25)
    assert result["cost"] == 3.25


async def test_set_purchase_cost_no_purchase_on_date_raises(bound_db) -> None:
    await server.record_purchase(items=[server.PurchaseLine(name="Eggs")], purchased_at=_DAY)
    with pytest.raises(ToolError, match="No purchase of 'Eggs' on 2026-08-02"):
        await server.set_purchase_cost("Eggs", "2026-08-02", 3.25)


async def test_set_purchase_store_tags_every_purchase_on_the_date(bound_db) -> None:
    await server.record_purchase(
        items=[
            server.PurchaseLine(name="Eggs", cost=2.5),
            server.PurchaseLine(name="Bread", cost=3.0),
        ],
        purchased_at=_DAY,
    )
    result = await server.set_purchase_store("Aldi", _DAY)
    assert result["updated_count"] == 2
    assert set(result["updated"]) == {"Eggs", "Bread"}


async def test_set_purchase_store_limited_to_item_names(bound_db) -> None:
    await server.record_purchase(
        items=[
            server.PurchaseLine(name="Eggs", cost=2.5),
            server.PurchaseLine(name="Bread", cost=3.0),
        ],
        purchased_at=_DAY,
    )
    result = await server.set_purchase_store("Aldi", _DAY, item_names=["Eggs"])
    assert result["updated"] == ["Eggs"]
    assert result["not_found"] == []


async def test_delete_purchase_removes_one_record(bound_db) -> None:
    await server.record_purchase(items=[server.PurchaseLine(name="Eggs")], purchased_at=_DAY)
    result = await server.delete_purchase("Eggs", _DAY)
    assert result == {"item": "Eggs", "deleted_purchase_date": _DAY}

    history = await server.get_item_history("Eggs")
    assert history["purchase_count"] == 0


async def test_delete_purchase_no_purchase_on_date_raises(bound_db) -> None:
    await server.record_purchase(items=[server.PurchaseLine(name="Eggs")], purchased_at=_DAY)
    with pytest.raises(ToolError, match="No purchase of 'Eggs' on 2026-08-02"):
        await server.delete_purchase("Eggs", "2026-08-02")


async def test_delete_item_dry_run_reports_and_does_not_delete(bound_db) -> None:
    await server.record_purchase(items=[server.PurchaseLine(name="Eggs")], purchased_at=_DAY)
    await server.record_purchase(
        items=[server.PurchaseLine(name="Eggs")], purchased_at="2026-08-02"
    )

    result = await server.delete_item("Eggs")
    assert result["confirm_required"] is True
    assert result["purchases_to_remove"] == 2

    history = await server.get_item_history("Eggs")
    assert history["purchase_count"] == 2


async def test_delete_item_confirmed_deletes_everything(bound_db) -> None:
    await server.record_purchase(items=[server.PurchaseLine(name="Eggs")], purchased_at=_DAY)

    result = await server.delete_item("Eggs", confirm=True)
    assert result == {"deleted": "Eggs", "purchases_removed": 1}

    with pytest.raises(ToolError, match="Eggs"):
        await server.get_item_history("Eggs")


async def test_add_alias_and_list_aliases(bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Granola Bars")], purchased_at=_DAY
    )

    add_result = await server.add_alias("Granola Bars", "Trail Mix Bars")
    assert add_result == {
        "item": "Granola Bars",
        "alias": "trail mix bars",
        "already_aliased": False,
    }

    one = await server.list_aliases("Granola Bars")
    assert one == {"item": "Granola Bars", "aliases": ["Trail Mix Bars"]}

    every = await server.list_aliases()
    assert every == {"items": [{"item": "Granola Bars", "aliases": ["Trail Mix Bars"]}]}


async def test_add_alias_conflict_raises_a_clear_error(bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Granola Bars")], purchased_at=_DAY
    )
    await server.record_purchase(items=[server.PurchaseLine(name="Yogurt")], purchased_at=_DAY)
    await server.add_alias("Granola Bars", "Trail Mix Bars")

    with pytest.raises(ToolError, match="already an alias of 'Granola Bars'"):
        await server.add_alias("Yogurt", "Trail Mix Bars")


async def test_add_alias_unknown_target_raises(bound_db) -> None:
    with pytest.raises(ToolError, match="Nonexistent"):
        await server.add_alias("Nonexistent", "Some Alias")


async def test_list_aliases_with_no_aliases_yet(bound_db) -> None:
    result = await server.list_aliases()
    assert result == {"items": []}


async def test_add_alias_is_a_no_op_when_already_aliased(bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Granola Bars")], purchased_at=_DAY
    )
    await server.add_alias("Granola Bars", "Trail Mix Bars")

    result = await server.add_alias("Granola Bars", "Trail Mix Bars")
    assert result == {
        "item": "Granola Bars",
        "alias": "trail mix bars",
        "already_aliased": True,
    }


async def test_add_alias_same_as_canonical_name_is_a_no_op(bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Granola Bars")], purchased_at=_DAY
    )

    result = await server.add_alias("Granola Bars", "granola bars")
    assert result["already_aliased"] is True


async def test_add_alias_target_that_is_already_a_tracked_item_raises(bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Granola Bars")], purchased_at=_DAY
    )
    await server.record_purchase(items=[server.PurchaseLine(name="Trail Mix")], purchased_at=_DAY)

    with pytest.raises(ToolError, match="already a tracked item"):
        await server.add_alias("Granola Bars", "Trail Mix")


async def test_set_purchase_store_reports_items_not_found(bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Eggs", cost=2.5)], purchased_at=_DAY
    )
    result = await server.set_purchase_store("Aldi", _DAY, item_names=["Eggs", "Nonexistent"])
    assert result["updated"] == ["Eggs"]
    assert result["not_found"] == ["Nonexistent"]


async def test_set_purchase_store_reports_item_with_no_purchase_on_date(bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Eggs", cost=2.5)], purchased_at=_DAY
    )
    result = await server.set_purchase_store("Aldi", "2026-08-02", item_names=["Eggs"])
    assert result["updated"] == []
    assert result["not_found"] == ["Eggs"]


async def test_consume_items_empty_list_rejected(bound_db) -> None:
    with pytest.raises(ToolError, match="consume_items needs at least one"):
        await server.consume_items([])


async def test_consume_items_blank_name_rejected(bound_db) -> None:
    with pytest.raises(ToolError, match="non-empty name"):
        await server.consume_items(["   "])


# --- P2.3: preferred products, price stats -------------------------------


async def test_record_purchase_links_a_product_from_a_upc(bound_db) -> None:
    result = await server.record_purchase(
        items=[
            server.PurchaseLine(
                name="Spaghetti Sauce",
                cost=4.5,
                upc="111",
                description="Rao's Marinara",
                size="24 oz",
            )
        ],
        purchased_at=_DAY,
    )
    product = result["items"][0]["product"]
    assert product["upc"] == "111"
    assert product["description"] == "Rao's Marinara"
    assert product["size"] == "24 oz"


async def test_record_purchase_upc_under_a_different_item_raises(bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Milk", upc="555")], purchased_at=_DAY
    )
    with pytest.raises(ToolError, match="Milk") as excinfo:
        await server.record_purchase(
            items=[server.PurchaseLine(name="Orange Juice", upc="555")],
            purchased_at="2026-08-02",
        )
    assert "Orange Juice" in str(excinfo.value)


async def test_resolve_product_unknown_item_raises(bound_db) -> None:
    with pytest.raises(ToolError, match="Nonexistent"):
        await server.resolve_product("Nonexistent")


async def test_resolve_product_no_preference_lists_candidates_most_recent_first(
    bound_db,
) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Spaghetti Sauce", upc="A")],
        purchased_at="2026-01-01",
    )
    await server.record_purchase(
        items=[server.PurchaseLine(name="Spaghetti Sauce", upc="B")],
        purchased_at="2026-01-03",
    )
    # A second sighting of "A" moves it back to most-recently-purchased.
    await server.record_purchase(
        items=[server.PurchaseLine(name="Spaghetti Sauce", upc="A")],
        purchased_at="2026-01-05",
    )

    result = await server.resolve_product("Spaghetti Sauce")

    assert result["preference"] is None
    assert [c["upc"] for c in result["candidates"]] == ["A", "B"]


async def test_set_preferred_product_pins_by_upc_and_resolve_reflects_it(bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Spaghetti Sauce", upc="A", description="Rao's")],
        purchased_at=_DAY,
    )

    pin_result = await server.set_preferred_product("Spaghetti Sauce", upc="A")
    assert pin_result["preference"]["upc"] == "A"
    assert pin_result["confidence"] == "auto"
    assert pin_result["source"] == "manual"
    assert pin_result["cleared"] is False

    resolved = await server.resolve_product("Spaghetti Sauce")
    assert resolved["preference"]["upc"] == "A"
    assert resolved["confidence"] == "auto"
    assert resolved["source"] == "manual"
    assert resolved.get("stale") is not True


async def test_set_preferred_product_pins_by_sku(bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Eggs", sku="SKU-1", store="Aldi")],
        purchased_at=_DAY,
    )

    result = await server.set_preferred_product("Eggs", sku="SKU-1")
    assert result["preference"]["sku"] == "SKU-1"


async def test_set_preferred_product_clears_with_no_args(bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Spaghetti Sauce", upc="A")],
        purchased_at=_DAY,
    )
    await server.set_preferred_product("Spaghetti Sauce", upc="A")

    result = await server.set_preferred_product("Spaghetti Sauce")
    assert result == {"item": "Spaghetti Sauce", "preference": None, "cleared": True}

    resolved = await server.resolve_product("Spaghetti Sauce")
    assert resolved["preference"] is None


async def test_set_preferred_product_unknown_item_raises(bound_db) -> None:
    with pytest.raises(ToolError, match="Nonexistent"):
        await server.set_preferred_product("Nonexistent", upc="A")


async def test_set_preferred_product_unmatched_upc_lists_known_products(bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Spaghetti Sauce", upc="A")],
        purchased_at=_DAY,
    )
    with pytest.raises(ToolError, match="Products on file: A") as excinfo:
        await server.set_preferred_product("Spaghetti Sauce", upc="Z")
    assert "Spaghetti Sauce" in str(excinfo.value)


async def test_resolve_product_staleness_guard_flips_on_when_preference_not_seen_recently(
    bound_db,
) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Spaghetti Sauce", upc="OLD")],
        purchased_at="2026-01-01",
    )
    await server.set_preferred_product("Spaghetti Sauce", upc="OLD")

    # Not stale yet: OLD is still within the last 2 purchases.
    fresh = await server.resolve_product("Spaghetti Sauce", recent_window=2)
    assert fresh.get("stale") is not True

    await server.record_purchase(
        items=[server.PurchaseLine(name="Spaghetti Sauce", upc="NEW")],
        purchased_at="2026-01-02",
    )
    await server.record_purchase(
        items=[server.PurchaseLine(name="Spaghetti Sauce", upc="NEW")],
        purchased_at="2026-01-03",
    )

    stale = await server.resolve_product("Spaghetti Sauce", recent_window=2)
    assert stale["stale"] is True
    assert "Spaghetti Sauce" in stale["note"]


async def test_get_price_stats_by_store_and_product_with_missing_costs(bound_db) -> None:
    await server.record_purchase(
        items=[
            server.PurchaseLine(
                name="Eggs", cost=2.0, upc="U1", store="Aldi", description="Grade A"
            )
        ],
        purchased_at="2026-01-01",
    )
    await server.record_purchase(
        items=[server.PurchaseLine(name="Eggs", cost=3.0, upc="U2", store="Kroger")],
        purchased_at="2026-01-02",
    )
    await server.record_purchase(
        items=[server.PurchaseLine(name="Eggs")],
        purchased_at="2026-01-03",
    )

    stats = await server.get_price_stats("Eggs")

    assert stats["overall"]["count"] == 3
    assert stats["overall"]["avg_cost"] == 2.5
    assert stats["overall"]["min_cost"] == 2.0
    assert stats["overall"]["max_cost"] == 3.0
    assert stats["overall"]["last_cost"] == 3.0  # most recent priced purchase

    by_store = {s["store"]: s for s in stats["by_store"]}
    assert by_store["Aldi"]["count"] == 1
    assert by_store["Aldi"]["avg_cost"] == 2.0
    assert by_store["unknown"]["count"] == 1
    assert by_store["unknown"]["avg_cost"] is None

    by_product = {p["upc"]: p for p in stats["by_product"]}
    assert by_product["U1"]["description"] == "Grade A"
    assert by_product["U1"]["avg_cost"] == 2.0
    assert by_product["U2"]["avg_cost"] == 3.0
    assert by_product[None]["count"] == 1
    assert by_product[None]["avg_cost"] is None


async def test_get_price_stats_never_errors_on_an_item_with_no_purchases(bound_db) -> None:
    await server.consume_items(["Kale"])

    stats = await server.get_price_stats("Kale")

    assert stats["overall"] == {
        "count": 0,
        "avg_cost": None,
        "min_cost": None,
        "max_cost": None,
        "last_cost": None,
    }
    assert stats["by_store"] == []
    assert stats["by_product"] == []


async def test_get_price_stats_unknown_item_raises(bound_db) -> None:
    with pytest.raises(ToolError, match="Nonexistent"):
        await server.get_price_stats("Nonexistent")


async def test_get_inventory_carries_preferred_product(bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Spaghetti Sauce", upc="A")],
        purchased_at=_DAY,
    )
    await server.set_preferred_product("Spaghetti Sauce", upc="A")

    rows = await server.get_inventory()
    row = next(r for r in rows if r["name"] == "Spaghetti Sauce")
    assert row["preferred_product"]["upc"] == "A"


async def test_get_inventory_preferred_product_is_null_by_default(bound_db) -> None:
    await server.record_purchase(items=[server.PurchaseLine(name="Eggs")], purchased_at=_DAY)

    rows = await server.get_inventory()
    row = next(r for r in rows if r["name"] == "Eggs")
    assert row["preferred_product"] is None


async def test_get_item_history_carries_preferred_product(bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Spaghetti Sauce", upc="A")],
        purchased_at=_DAY,
    )
    await server.set_preferred_product("Spaghetti Sauce", upc="A")

    history = await server.get_item_history("Spaghetti Sauce")
    assert history["preferred_product"]["upc"] == "A"
