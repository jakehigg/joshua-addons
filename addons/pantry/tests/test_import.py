"""``import_data``: the full fixture, idempotent re-import, chunking, batch
validation, conflict reporting, and atomicity. See docs/import.md.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from joshua_pantry import server
from joshua_pantry.database import build_engine, build_sessionmaker
from joshua_pantry.migrations import run_migrations
from mcp.server.mcpserver.exceptions import ToolError

FIXTURES = Path(__file__).parent / "fixtures"

_ALL_STATUSES = {"in_stock", "likely_depleted", "out_of_stock", "unknown"}


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def _chunks(rows: list[Any], size: int) -> list[list[Any]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)]


def _strip_volatile(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop fields that legitimately differ between two runs a moment apart."""
    return [{k: v for k, v in row.items() if k != "updated_at"} for row in rows]


async def test_import_full_fixture_populates_inventory_and_products(bound_db) -> None:
    data = _load_fixture("import_sample.json")

    result = await server.import_data(**data)

    assert result["counts"]["items"] == {"created": 10, "merged": 0, "skipped": 0}
    assert result["counts"]["purchases"] == {"created": 40, "merged": 0, "skipped": 0}
    assert result["counts"]["consumptions"] == {"created": 10, "skipped": 0}
    assert result["conflicts"] == []

    inventory = await server.get_inventory()
    assert {row["name"] for row in inventory} == {
        "Milk",
        "Eggs",
        "Spaghetti Sauce",
        "Bread",
        "Butter",
        "Coffee",
        "Bananas",
        "Chicken Thighs",
        "Rice",
        "Olive Oil",
    }
    for row in inventory:
        assert row["status"] in _ALL_STATUSES

    # The consumption dated after Bananas' last purchase drives it out of stock.
    bananas = next(row for row in inventory if row["name"] == "Bananas")
    assert bananas["status"] == "out_of_stock"

    # The imported akas resolve like a live receipt name would.
    milk_history = await server.get_item_history("whole milk")
    assert milk_history["item"] == "Milk"
    assert milk_history["matched_via"] == "alias"

    # The upc-only preference.
    sauce = await server.resolve_product("Spaghetti Sauce")
    assert sauce["preference"]["upc"] == "00111122223"
    assert sauce["confidence"] == "auto"
    assert sauce["source"] == "imported"

    # The sku-only preference.
    coffee = await server.resolve_product("Coffee")
    assert coffee["preference"]["sku"] == "SKU-COFFEE-1"
    assert coffee["confidence"] == "low"
    assert coffee["source"] == "imported"

    # Purchases split across the two stores in the fixture.
    egg_stats = await server.get_price_stats("Eggs")
    assert {s["store"] for s in egg_stats["by_store"]} == {"Aldi", "Kroger"}


async def test_import_is_idempotent_on_rerun(bound_db) -> None:
    data = _load_fixture("import_sample.json")

    await server.import_data(**data)
    before = _strip_volatile(await server.get_inventory())

    result = await server.import_data(**data)

    assert result["counts"]["items"]["created"] == 0
    assert result["counts"]["purchases"]["created"] == 0
    assert result["counts"]["consumptions"]["created"] == 0
    assert result["conflicts"] == []

    after = _strip_volatile(await server.get_inventory())
    assert after == before


async def test_chunked_import_equals_single_shot_import(tmp_path) -> None:
    import joshua_pantry.server as server_module

    data = _load_fixture("import_sample.json")

    chunked_engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'chunked.db'}")
    single_engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'single.db'}")
    await run_migrations(chunked_engine)
    await run_migrations(single_engine)
    chunked_sm = build_sessionmaker(chunked_engine)
    single_sm = build_sessionmaker(single_engine)

    previous = server_module._sessionmaker
    try:
        server_module._sessionmaker = chunked_sm
        for chunk in _chunks(data["items"], 3):
            await server.import_data(version=1, items=chunk)
        for chunk in _chunks(data["purchases"], 9):
            await server.import_data(version=1, purchases=chunk)
        for chunk in _chunks(data["consumptions"], 4):
            await server.import_data(version=1, consumptions=chunk)
        chunked_inventory = _strip_volatile(await server.get_inventory())

        server_module._sessionmaker = single_sm
        await server.import_data(**data)
        single_inventory = _strip_volatile(await server.get_inventory())
    finally:
        server_module._sessionmaker = previous
        await chunked_engine.dispose()
        await single_engine.dispose()

    assert chunked_inventory == single_inventory


# --- validation: whole batch checked first, nothing applied on error --------


async def test_unknown_field_names_the_json_path(bound_db) -> None:
    with pytest.raises(ToolError, match=r"items\[0\]: unknown field 'bogus'"):
        await server.import_data(version=1, items=[{"name": "Milk", "bogus": True}])
    assert await server.get_inventory() == []


async def test_bad_row_names_section_and_index(bound_db) -> None:
    with pytest.raises(ToolError, match=r"purchases\[1\]\.item"):
        await server.import_data(
            version=1,
            purchases=[
                {"item": "Milk", "purchased_at": "2026-01-01"},
                {"item": "", "purchased_at": "2026-01-01"},
            ],
        )
    assert await server.get_inventory() == []


async def test_unparseable_date_names_section_and_index(bound_db) -> None:
    with pytest.raises(ToolError, match=r"consumptions\[0\]\.occurred_at"):
        await server.import_data(
            version=1, consumptions=[{"item": "Milk", "occurred_at": "not-a-date"}]
        )


async def test_preference_without_upc_or_sku_is_rejected(bound_db) -> None:
    with pytest.raises(ToolError, match=r"items\[0\]\.preference: needs upc or sku"):
        await server.import_data(
            version=1,
            items=[{"name": "Tea", "preference": {"confidence": "auto"}}],
        )
    assert await server.get_inventory() == []


async def test_aka_collision_with_an_existing_item_is_rejected(bound_db) -> None:
    await server.import_data(**_load_fixture("import_sample.json"))

    collision = _load_fixture("import_sample_aka_collision.json")
    with pytest.raises(ToolError, match="Milk"):
        await server.import_data(**collision)

    # Nothing from the rejected batch was created.
    names = {row["name"] for row in await server.get_inventory()}
    assert "Cereal" not in names
    assert "Almond Milk" not in names


async def test_aka_collision_names_both_items(bound_db) -> None:
    await server.import_data(version=1, items=[{"name": "Sugar", "akas": ["white sugar"]}])

    with pytest.raises(ToolError, match="Sugar") as excinfo:
        await server.import_data(version=1, items=[{"name": "Flour", "akas": ["white sugar"]}])
    assert "Flour" in str(excinfo.value)


async def test_version_mismatch_is_rejected(bound_db) -> None:
    with pytest.raises(ToolError, match="version"):
        await server.import_data(version=2, items=[{"name": "Milk"}])


async def test_blank_item_name_is_rejected_and_stops_the_row(bound_db) -> None:
    with pytest.raises(ToolError, match=r"items\[0\]\.name: required"):
        await server.import_data(version=1, items=[{"akas": ["milk"]}])


async def test_non_object_row_is_rejected_in_every_section(bound_db) -> None:
    with pytest.raises(ToolError, match=r"items\[0\]: must be an object"):
        await server.import_data(version=1, items=["not an object"])
    with pytest.raises(ToolError, match=r"purchases\[0\]: must be an object"):
        await server.import_data(version=1, purchases=["not an object"])
    with pytest.raises(ToolError, match=r"consumptions\[0\]: must be an object"):
        await server.import_data(version=1, consumptions=["not an object"])


async def test_unknown_field_is_rejected_in_purchases_and_consumptions(bound_db) -> None:
    with pytest.raises(ToolError, match=r"purchases\[0\]: unknown field 'brand'"):
        await server.import_data(
            version=1,
            purchases=[{"item": "Milk", "purchased_at": "2026-01-01", "brand": "x"}],
        )
    with pytest.raises(ToolError, match=r"consumptions\[0\]: unknown field 'brand'"):
        await server.import_data(
            version=1, consumptions=[{"item": "Milk", "occurred_at": "2026-01-01", "brand": "x"}]
        )


async def test_non_numeric_cost_is_rejected(bound_db) -> None:
    with pytest.raises(ToolError, match=r"purchases\[0\]\.cost: must be a number"):
        await server.import_data(
            version=1,
            purchases=[{"item": "Milk", "purchased_at": "2026-01-01", "cost": "free"}],
        )


async def test_akas_must_be_a_list(bound_db) -> None:
    with pytest.raises(ToolError, match=r"items\[0\]\.akas: must be a list"):
        await server.import_data(version=1, items=[{"name": "Milk", "akas": "whole milk"}])


async def test_blank_aka_entry_is_rejected(bound_db) -> None:
    with pytest.raises(ToolError, match=r"items\[0\]\.akas\[0\]: must be a non-empty string"):
        await server.import_data(version=1, items=[{"name": "Milk", "akas": ["  "]}])


async def test_preference_must_be_an_object(bound_db) -> None:
    with pytest.raises(ToolError, match=r"items\[0\]\.preference: must be an object"):
        await server.import_data(version=1, items=[{"name": "Milk", "preference": "A"}])


async def test_preference_rejects_an_unknown_field(bound_db) -> None:
    with pytest.raises(ToolError, match=r"items\[0\]\.preference: unknown field 'brand'"):
        await server.import_data(
            version=1,
            items=[
                {"name": "Milk", "preference": {"upc": "1", "confidence": "auto", "brand": "x"}}
            ],
        )


async def test_preference_rejects_a_bad_confidence_value(bound_db) -> None:
    with pytest.raises(ToolError, match=r"items\[0\]\.preference\.confidence"):
        await server.import_data(
            version=1, items=[{"name": "Milk", "preference": {"upc": "1", "confidence": "maybe"}}]
        )


async def test_self_aliasing_an_items_own_name_is_a_no_op(bound_db) -> None:
    result = await server.import_data(version=1, items=[{"name": "Milk", "akas": ["milk"]}])
    assert result["counts"]["items"] == {"created": 1, "merged": 0, "skipped": 0}
    assert (await server.list_aliases("Milk"))["aliases"] == []


async def test_within_batch_aka_collision_is_rejected(bound_db) -> None:
    with pytest.raises(ToolError, match="Sugar") as excinfo:
        await server.import_data(
            version=1,
            items=[
                {"name": "Sugar", "akas": ["white sugar"]},
                {"name": "Flour", "akas": ["white sugar"]},
            ],
        )
    assert "Flour" in str(excinfo.value)
    assert await server.get_inventory() == []


async def test_purchases_only_import_creates_and_updates_the_item(bound_db) -> None:
    result = await server.import_data(
        version=1,
        purchases=[
            {"item": "New Item", "purchased_at": "2026-01-01"},
            {"item": "New Item", "purchased_at": "2026-01-15"},
        ],
    )
    assert result["counts"]["items"] == {"created": 1, "merged": 0, "skipped": 0}
    assert result["counts"]["purchases"] == {"created": 2, "merged": 0, "skipped": 0}

    history = await server.get_item_history("New Item")
    assert history["purchase_count"] == 2


async def test_consumptions_only_import_creates_the_item(bound_db) -> None:
    result = await server.import_data(
        version=1, consumptions=[{"item": "Kale", "occurred_at": "2026-01-01T12:00:00Z"}]
    )
    assert result["counts"]["items"] == {"created": 1, "merged": 0, "skipped": 0}
    assert result["counts"]["consumptions"] == {"created": 1, "skipped": 0}

    names = {row["name"] for row in await server.get_inventory()}
    assert "Kale" in names


# --- conflicts ---------------------------------------------------------


async def test_manual_preference_is_never_overwritten_and_is_reported(bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Spaghetti Sauce", upc="A")],
        purchased_at="2026-01-01",
    )
    await server.set_preferred_product("Spaghetti Sauce", upc="A")

    result = await server.import_data(
        version=1,
        items=[
            {
                "name": "Spaghetti Sauce",
                "preference": {"upc": "B", "confidence": "auto"},
            }
        ],
    )

    assert result["conflicts"] == [
        {
            "type": "preference",
            "item": "Spaghetti Sauce",
            "reason": "an existing manual preference is never overwritten",
            "existing": result["conflicts"][0]["existing"],
            "imported": {"upc": "B"},
        }
    ]
    assert result["conflicts"][0]["existing"]["upc"] == "A"

    resolved = await server.resolve_product("Spaghetti Sauce")
    assert resolved["preference"]["upc"] == "A"
    assert resolved["source"] == "manual"


async def test_category_conflict_is_reported_and_does_not_overwrite(bound_db) -> None:
    await server.import_data(version=1, items=[{"name": "Milk", "category": "Dairy"}])

    result = await server.import_data(version=1, items=[{"name": "Milk", "category": "Beverages"}])

    assert result["conflicts"] == [
        {
            "type": "item_category",
            "item": "Milk",
            "existing": "Dairy",
            "imported": "Beverages",
        }
    ]
    inventory = await server.get_inventory()
    milk = next(row for row in inventory if row["name"] == "Milk")
    assert milk["category"] == "Dairy"


async def test_preferred_store_conflict_is_reported_and_does_not_overwrite(bound_db) -> None:
    await server.import_data(version=1, items=[{"name": "Milk", "preferred_store": "Aldi"}])

    result = await server.import_data(
        version=1, items=[{"name": "Milk", "preferred_store": "Kroger"}]
    )

    assert result["conflicts"] == [
        {
            "type": "item_preferred_store",
            "item": "Milk",
            "existing": "Aldi",
            "imported": "Kroger",
        }
    ]
    history = await server.get_item_history("Milk")
    assert history["preferred_store"] == "Aldi"


async def test_null_field_takes_the_imported_value_without_conflict(bound_db) -> None:
    await server.import_data(version=1, items=[{"name": "Milk"}])

    result = await server.import_data(
        version=1, items=[{"name": "Milk", "preferred_store": "Aldi"}]
    )

    assert result["conflicts"] == []
    assert result["counts"]["items"] == {"created": 0, "merged": 1, "skipped": 0}


# --- atomicity -----------------------------------------------------------


async def test_apply_phase_failure_rolls_back_the_whole_batch(bound_db) -> None:
    """A upc collision discovered mid-apply rolls back everything else in
    the same call, including the item creation that came before it.
    """
    await server.record_purchase(
        items=[server.PurchaseLine(name="Milk", upc="555")], purchased_at="2026-01-01"
    )

    with pytest.raises(ToolError, match="555"):
        await server.import_data(
            version=1,
            items=[{"name": "Orange Juice"}],
            purchases=[{"item": "Orange Juice", "purchased_at": "2026-01-02", "upc": "555"}],
        )

    names = {row["name"] for row in await server.get_inventory()}
    assert names == {"Milk"}


async def test_mutated_fixture_with_one_bad_row_applies_nothing(bound_db) -> None:
    data = copy.deepcopy(_load_fixture("import_sample.json"))
    data["purchases"][-1]["purchased_at"] = "not-a-date"

    with pytest.raises(ToolError):
        await server.import_data(**data)

    assert await server.get_inventory() == []
