"""``GET /api/categories``: the alphabetical list with tracked-item counts."""

from __future__ import annotations

import httpx
from joshua_pantry import categories, server
from joshua_pantry.models import Item
from joshua_pantry.resolution import resolve_or_create_category
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def test_list_categories_counts_only_tracked_items(session: AsyncSession) -> None:
    dairy = await resolve_or_create_category(session, "Dairy")
    tracked = Item(name="milk", normalized="milk", category_id=dairy.id, is_tracked=True)
    hidden = Item(name="cream", normalized="cream", category_id=dairy.id, is_tracked=False)
    session.add_all([tracked, hidden])
    await session.commit()

    rows = await categories.list_categories(session)

    assert rows == [{"id": dairy.id, "name": "dairy", "item_count": 1}]


async def test_list_categories_empty_database(session: AsyncSession) -> None:
    assert await categories.list_categories(session) == []


async def test_get_categories_reports_tracked_item_counts(app_factory, bound_db) -> None:
    await server.record_purchase(
        items=[server.PurchaseLine(name="Bread")], purchased_at="2026-08-01"
    )
    await server.record_purchase(
        items=[server.PurchaseLine(name="Milk")], purchased_at="2026-08-01"
    )

    async with server._session_factory()() as session:
        category = await resolve_or_create_category(session, "Dairy")
        milk = (
            (await session.execute(select(Item).where(Item.normalized == "milk"))).scalars().first()
        )
        milk.category_id = category.id
        await session.commit()

    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/categories")

    assert response.status_code == 200
    body = response.json()
    dairy = next(c for c in body if c["name"] == "Dairy")
    assert dairy["item_count"] == 1


async def test_get_categories_empty_when_none_assigned(app_factory, bound_db) -> None:
    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/categories")
    assert response.status_code == 200
    assert response.json() == []
