"""When ``ADDON_TOKEN`` is set, the MCP route needs a matching bearer token."""

from __future__ import annotations

import httpx
import pytest
from conftest import mcp_session


@pytest.mark.asyncio
async def test_mcp_route_rejects_a_wrong_token(app_factory) -> None:
    app = app_factory("right-token")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/mcp", headers={"Authorization": "Bearer wrong-token"}, json={}
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_mcp_route_rejects_a_missing_token(app_factory) -> None:
    app = app_factory("right-token")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/mcp", json={})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_mcp_route_accepts_the_right_token(app_factory) -> None:
    app = app_factory("right-token")
    async with mcp_session(app, headers={"Authorization": "Bearer right-token"}) as session:
        result = await session.call_tool("hello", {"name": "Alex"})
        assert result.is_error is not True
        [block] = result.content
        assert block.text == "Hello, Alex!"
