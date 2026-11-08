"""When ``ADDON_TOKEN`` is set, ``/mcp`` needs a matching bearer token."""

from __future__ import annotations

import httpx
import pytest
from conftest import mcp_session


async def _post_mcp(app, headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post("/mcp", headers=headers or {}, json={})


async def test_mcp_rejects_a_wrong_token(app_factory) -> None:
    response = await _post_mcp(app_factory("right-token"), {"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401


async def test_mcp_rejects_a_missing_token(app_factory) -> None:
    assert (await _post_mcp(app_factory("right-token"))).status_code == 401


async def test_mcp_rejects_a_non_bearer_scheme(app_factory) -> None:
    response = await _post_mcp(app_factory("right-token"), {"Authorization": "right-token"})
    assert response.status_code == 401


@pytest.mark.parametrize("token", [None, ""])
async def test_mcp_is_open_when_no_token_is_set(app_factory, token) -> None:
    app = app_factory(token)
    async with mcp_session(app) as session:
        result = await session.call_tool("ping", {})
    assert result.is_error is not True


async def test_mcp_accepts_the_right_token(app_factory) -> None:
    app = app_factory("right-token")
    async with mcp_session(app, headers={"Authorization": "Bearer right-token"}) as session:
        result = await session.call_tool("ping", {})
    assert result.is_error is not True
