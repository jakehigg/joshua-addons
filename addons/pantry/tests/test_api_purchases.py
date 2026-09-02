"""``/api/purchases``: pagination, partial-update (with null-clears) PATCH, and
DELETE-triggers-recalculation.
"""

from __future__ import annotations

import httpx
from joshua_pantry import server
from joshua_pantry.models import PurchaseRecord
from sqlalchemy import select


async def test_list_purchases_paginates_and_reports_total(app_factory, bound_db) -> None:
    for i, day in enumerate(["2026-08-01", "2026-08-02", "2026-08-03"]):
        await server.record_purchase(
            items=[server.PurchaseLine(name=f"Item{i}", cost=1.0)], purchased_at=day
        )

    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.get("/api/purchases", params={"limit": 2, "offset": 0})
        second = await client.get("/api/purchases", params={"limit": 2, "offset": 2})

    assert first.status_code == 200
    body = first.json()
    assert body["total"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert len(body["purchases"]) == 2
    # Newest first: the most recent purchase (2026-08-03) leads the page.
    assert body["purchases"][0]["item_name"] == "Item2"

    assert second.status_code == 200
    assert len(second.json()["purchases"]) == 1


async def test_list_purchases_rejects_out_of_range_limit(app_factory, bound_db) -> None:
    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/purchases", params={"limit": 501})
    assert response.status_code == 400


async def _seed_one_purchase(store: str = "Aldi", cost: float = 2.5) -> int:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Eggs", cost=cost, store=store)],
        purchased_at="2026-08-01",
    )
    async with server._session_factory()() as session:
        rec = (await session.execute(select(PurchaseRecord))).scalars().first()
        assert rec is not None
        return rec.id


async def test_patch_purchase_partial_update_only_changes_sent_field(app_factory, bound_db) -> None:
    purchase_id = await _seed_one_purchase(store="Aldi", cost=2.5)

    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.patch(f"/api/purchases/{purchase_id}", json={"unit_cost": 5.0})

    assert response.status_code == 200
    body = response.json()
    assert body["unit_cost"] == 5.0
    assert body["store"] == "Aldi"  # untouched: not sent


async def test_patch_purchase_null_clears_the_column(app_factory, bound_db) -> None:
    purchase_id = await _seed_one_purchase(store="Aldi", cost=2.5)

    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.patch(f"/api/purchases/{purchase_id}", json={"store": None})

    assert response.status_code == 200
    body = response.json()
    assert body["store"] is None
    assert body["unit_cost"] == 2.5  # untouched: not sent


async def test_patch_purchase_no_fields_is_400(app_factory, bound_db) -> None:
    purchase_id = await _seed_one_purchase()

    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.patch(f"/api/purchases/{purchase_id}", json={})

    assert response.status_code == 400
    assert response.json() == {"detail": "No fields to update"}


async def test_patch_purchase_not_found_is_404(app_factory, bound_db) -> None:
    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.patch("/api/purchases/999", json={"unit_cost": 1.0})
    assert response.status_code == 404


async def test_delete_purchase_recomputes_inventory(app_factory, bound_db) -> None:
    # Four purchases, 2 days apart: three intervals of 2 days, enough for the
    # v2 algorithm's min_intervals=3 to compute avg_cycle_days == 2.0.
    for day in ["2026-08-01", "2026-08-03", "2026-08-05", "2026-08-07"]:
        await server.record_purchase(
            items=[server.PurchaseLine(name="Eggs", cost=2.5)], purchased_at=day
        )

    async with server._session_factory()() as session:
        rows = (
            (await session.execute(select(PurchaseRecord).order_by(PurchaseRecord.purchased_at)))
            .scalars()
            .all()
        )
    assert len(rows) == 4
    latest_id = rows[-1].id

    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        before = await client.get("/api/inventory")
        delete_response = await client.delete(f"/api/purchases/{latest_id}")
        after = await client.get("/api/inventory")

    assert before.json()[0]["avg_cycle_days"] == 2.0

    assert delete_response.status_code == 200
    assert delete_response.json() == {"ok": True}

    # Three purchases remain: only two intervals, below min_intervals=3, so
    # the recalculation drops avg_cycle_days back to null.
    assert after.json()[0]["avg_cycle_days"] is None


async def test_delete_purchase_not_found_is_404(app_factory, bound_db) -> None:
    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.delete("/api/purchases/999")
    assert response.status_code == 404
