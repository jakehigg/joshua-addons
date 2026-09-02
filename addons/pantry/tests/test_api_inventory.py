"""``GET /api/inventory`` and ``GET /api/analytics``: the ported UI's read routes."""

from __future__ import annotations

import httpx
from joshua_pantry import server


async def test_get_inventory_lists_recorded_items(app_factory, bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Eggs", cost=2.5)], purchased_at="2026-08-01"
    )

    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/inventory")

    assert response.status_code == 200
    rows = response.json()
    assert [r["name"] for r in rows] == ["Eggs"]
    assert rows[0]["status"] in {"in_stock", "unknown"}


async def test_get_inventory_status_filter(app_factory, bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Eggs", cost=2.5)], purchased_at="2026-08-01"
    )

    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/inventory", params={"status": "out_of_stock"})

    assert response.status_code == 200
    assert response.json() == []


async def test_get_analytics_reports_purchase_count(app_factory, bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Eggs", cost=2.0)], purchased_at="2026-08-01"
    )
    await server.record_purchase(
        items=[server.PurchaseLine(name="Eggs", cost=4.0)], purchased_at="2026-08-08"
    )

    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/analytics")

    assert response.status_code == 200
    rows = response.json()
    assert rows[0]["name"] == "Eggs"
    assert rows[0]["purchase_count"] == 2
    assert rows[0]["avg_cost"] == 3.0
