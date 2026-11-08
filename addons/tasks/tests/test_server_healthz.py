"""``GET /healthz`` stays open, with or without ``ADDON_TOKEN``."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.parametrize("token", [None, "secret-token"])
async def test_healthz_ok(app_factory, token) -> None:
    app = app_factory(token)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


async def test_api_mount_is_empty(app_factory) -> None:
    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/anything")
    assert response.status_code == 404
