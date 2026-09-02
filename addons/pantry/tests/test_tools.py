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
