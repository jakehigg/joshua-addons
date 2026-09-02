"""``/api/items/*`` and the two ``/api/inventory/*`` item-action routes."""

from __future__ import annotations

import httpx
from joshua_pantry import server
from joshua_pantry.models import Item
from sqlalchemy import select


async def _item_id(name: str) -> int:
    async with server._session_factory()() as session:
        item = (
            (await session.execute(select(Item).where(Item.normalized == name.lower())))
            .scalars()
            .first()
        )
        assert item is not None
        return item.id


async def test_alias_add_list_remove_round_trip(app_factory, bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Granola Bars", cost=3.0)], purchased_at="2026-08-01"
    )
    item_id = await _item_id("Granola Bars")

    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        empty = await client.get(f"/api/items/{item_id}/aliases")
        assert empty.status_code == 200
        assert empty.json() == []

        added = await client.post(f"/api/items/{item_id}/aliases", json={"alias": "Trail Mix Bars"})
        assert added.status_code == 200
        alias_id = added.json()["id"]
        assert added.json()["alias"] == "trail mix bars"

        listed = await client.get(f"/api/items/{item_id}/aliases")
        assert [a["alias"] for a in listed.json()] == ["trail mix bars"]

        removed = await client.delete(f"/api/items/{item_id}/aliases/{alias_id}")
        assert removed.status_code == 200
        assert removed.json() == {"ok": True}

        after = await client.get(f"/api/items/{item_id}/aliases")
        assert after.json() == []


async def test_add_alias_requires_a_non_empty_name(app_factory, bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Bread")], purchased_at="2026-08-01"
    )
    item_id = await _item_id("Bread")

    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(f"/api/items/{item_id}/aliases", json={"alias": "  "})
    assert response.status_code == 400


async def test_remove_alias_not_found_is_404(app_factory, bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Bread")], purchased_at="2026-08-01"
    )
    item_id = await _item_id("Bread")

    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.delete(f"/api/items/{item_id}/aliases/999")
    assert response.status_code == 404


async def test_merge_items_combines_history_and_adds_alias(app_factory, bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Bread")], purchased_at="2026-08-01"
    )
    await server.record_purchase(
        items=[server.PurchaseLine(name="Milk")], purchased_at="2026-08-02"
    )
    source_id = await _item_id("Bread")
    target_id = await _item_id("Milk")

    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(f"/api/items/{source_id}/merge-into/{target_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["target_name"] == "Milk"
    assert body["alias_added"] == "bread"
    assert body["moved_purchases"] == 1


async def test_merge_items_same_id_is_400(app_factory, bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Bread")], purchased_at="2026-08-01"
    )
    item_id = await _item_id("Bread")

    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(f"/api/items/{item_id}/merge-into/{item_id}")
    assert response.status_code == 400


async def test_merge_items_missing_item_is_404(app_factory, bound_db) -> None:
    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/items/1/merge-into/2")
    assert response.status_code == 404


async def test_rename_item_updates_name_and_aliases_the_old_one(app_factory, bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Bread")], purchased_at="2026-08-01"
    )
    item_id = await _item_id("Bread")

    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(f"/api/items/{item_id}/rename", json={"new_name": "Sourdough"})
        assert response.status_code == 200
        assert response.json() == {"id": item_id, "name": "Sourdough"}

        aliases = await client.get(f"/api/items/{item_id}/aliases")
    assert [a["alias"] for a in aliases.json()] == ["bread"]


async def test_rename_item_conflict_is_400(app_factory, bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Bread")], purchased_at="2026-08-01"
    )
    await server.record_purchase(
        items=[server.PurchaseLine(name="Milk")], purchased_at="2026-08-01"
    )
    item_id = await _item_id("Bread")

    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(f"/api/items/{item_id}/rename", json={"new_name": "Milk"})
    assert response.status_code == 400


async def test_deactivate_item_hides_it_from_inventory(app_factory, bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Bread")], purchased_at="2026-08-01"
    )
    item_id = await _item_id("Bread")

    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.delete(f"/api/items/{item_id}")
        assert response.status_code == 200
        assert response.json() == {"ok": True}

        inventory = await client.get("/api/inventory")
    assert inventory.json() == []


async def test_deactivate_item_not_found_is_404(app_factory, bound_db) -> None:
    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.delete("/api/items/999")
    assert response.status_code == 404


async def test_mark_out_of_stock_recalculates_status(app_factory, bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Bread")], purchased_at="2026-08-01"
    )
    item_id = await _item_id("Bread")

    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(f"/api/inventory/{item_id}/mark-out-of-stock")
        assert response.status_code == 200
        assert response.json() == {"item_name": "Bread"}

        inventory = await client.get("/api/inventory")
    assert inventory.json()[0]["status"] == "out_of_stock"


async def test_mark_out_of_stock_not_found_is_404(app_factory, bound_db) -> None:
    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/inventory/999/mark-out-of-stock")
    assert response.status_code == 404


async def test_record_purchase_route_creates_a_new_item(app_factory, bound_db) -> None:
    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/inventory/record-purchase", json={"name": "Bread"})
    assert response.status_code == 200
    assert response.json() == {"item_name": "Bread", "created": True}


async def test_record_purchase_route_reuses_an_existing_item(app_factory, bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Bread")], purchased_at="2026-08-01"
    )

    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/inventory/record-purchase", json={"name": "Bread"})
    assert response.status_code == 200
    assert response.json() == {"item_name": "Bread", "created": False}


async def test_record_purchase_route_requires_a_name(app_factory, bound_db) -> None:
    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/inventory/record-purchase", json={})
    assert response.status_code == 400
