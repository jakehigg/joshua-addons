"""``/api/consumptions``: pagination, and DELETE-undoes-the-consumption."""

from __future__ import annotations

import httpx
from joshua_pantry import server


async def _client(app_factory):
    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def test_list_consumptions_paginates_and_reports_total(app_factory, bound_db) -> None:
    await server.consume_items(["Eggs", "Bread", "Kale"], note="breakfast")

    client = await _client(app_factory)
    async with client:
        first = await client.get("/api/consumptions", params={"limit": 2, "offset": 0})
        second = await client.get("/api/consumptions", params={"limit": 2, "offset": 2})

    assert first.status_code == 200
    body = first.json()
    assert body["total"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert len(body["consumptions"]) == 2
    assert body["consumptions"][0]["note"] == "breakfast"
    assert body["consumptions"][0]["quantity"] is None

    assert second.status_code == 200
    assert len(second.json()["consumptions"]) == 1


async def test_list_consumptions_rejects_out_of_range_limit(app_factory, bound_db) -> None:
    client = await _client(app_factory)
    async with client:
        response = await client.get("/api/consumptions", params={"limit": 501})
    assert response.status_code == 400
    assert response.json() == {"detail": "limit must be between 1 and 500"}


async def test_delete_consumption_puts_the_item_back_in_stock(app_factory, bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Eggs", cost=2.5)], purchased_at="2026-08-01"
    )
    await server.consume_items(["Eggs"], note="omelette")

    client = await _client(app_factory)
    async with client:
        listed = await client.get("/api/consumptions")
        event_id = listed.json()["consumptions"][0]["id"]
        before = await client.get("/api/inventory")
        delete_response = await client.delete(f"/api/consumptions/{event_id}")
        after = await client.get("/api/inventory")

    assert before.json()[0]["status"] == "out_of_stock"

    assert delete_response.status_code == 200
    assert delete_response.json() == {"ok": True}

    # The purchase stays on file, and nothing empties the stock any more.
    assert after.json()[0]["status"] != "out_of_stock"
    assert after.json()[0]["last_purchased_at"].startswith("2026-08-01")


async def test_delete_consumption_not_found_is_404(app_factory, bound_db) -> None:
    client = await _client(app_factory)
    async with client:
        response = await client.delete("/api/consumptions/999")
    assert response.status_code == 404
