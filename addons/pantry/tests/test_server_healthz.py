"""``GET /healthz`` stays open, with or without ``ADDON_TOKEN``."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_healthz_ok_with_no_token(app_factory) -> None:
    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_healthz_ok_with_a_token_configured_and_no_auth_header(app_factory) -> None:
    app = app_factory("secret-token")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
