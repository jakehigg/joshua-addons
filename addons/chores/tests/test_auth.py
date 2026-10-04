"""When ``ADDON_TOKEN`` is set, ``/mcp`` needs a matching bearer token."""

from __future__ import annotations

import httpx
import pytest
from conftest import api_client, mcp_session
from joshua_chores import server


async def _post_mcp(app, headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post("/mcp", headers=headers or {}, json={})


async def test_mcp_rejects_a_missing_token(app_factory) -> None:
    response = await _post_mcp(app_factory("right-token"))
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


async def test_mcp_rejects_a_wrong_token(app_factory) -> None:
    response = await _post_mcp(
        app_factory("right-token"), headers={"Authorization": "Bearer wrong-token"}
    )
    assert response.status_code == 401


async def test_mcp_rejects_a_token_without_the_bearer_scheme(app_factory) -> None:
    response = await _post_mcp(app_factory("right-token"), headers={"Authorization": "right-token"})
    assert response.status_code == 401


async def test_mcp_subpath_needs_the_token(app_factory) -> None:
    app = app_factory("right-token")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/mcp/anything", json={})
    assert response.status_code == 401


async def test_mcp_accepts_the_right_token(app_factory) -> None:
    app = app_factory("right-token")
    async with mcp_session(app, headers={"Authorization": "Bearer right-token"}) as session:
        result = await session.call_tool("list_members", {})
    assert result.is_error is not True
    assert result.structured_content == {"members": []}


async def test_mcp_is_open_when_no_token_is_set(app_factory) -> None:
    app = app_factory(None)
    async with mcp_session(app) as session:
        result = await session.call_tool("list_members", {})
    assert result.structured_content == {"members": []}


async def test_healthz_stays_open_when_a_token_is_set(app_factory) -> None:
    app = app_factory("right-token")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_session_factory_fails_outside_the_lifespan() -> None:
    with pytest.raises(RuntimeError, match="not ready"):
        server._session_factory()


async def test_mcp_token_does_not_guard_the_api(app_factory, monkeypatch) -> None:
    monkeypatch.setenv("MANAGER_PIN", "test-pin")
    app = app_factory("right-token")
    async with api_client(app) as client:
        mcp = await client.post("/mcp", json={})
        members = await client.get("/api/members")
        token_on_api = await client.post(
            "/api/members",
            json={"slug": "alpha", "name": "Alpha"},
            headers={"Authorization": "Bearer right-token"},
        )
    assert mcp.status_code == 401
    assert members.status_code == 200
    assert members.json() == []
    assert token_on_api.status_code == 401


async def test_manager_pin_does_not_open_mcp(app_factory, monkeypatch) -> None:
    monkeypatch.setenv("MANAGER_PIN", "test-pin")
    app = app_factory("right-token")
    async with api_client(app) as client:
        response = await client.post("/mcp", json={}, headers={"Authorization": "Bearer test-pin"})
    assert response.status_code == 401
