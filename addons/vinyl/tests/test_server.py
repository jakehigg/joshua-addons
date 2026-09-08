"""The server: the open routes, the token on ``/mcp``, the bundle, the art, and the UI."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from conftest import mcp_session
from joshua_vinyl import server


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def test_healthz_ok_with_no_token(app_factory) -> None:
    async with _client(app_factory(None)) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


async def test_healthz_stays_open_with_a_token_configured(app_factory) -> None:
    async with _client(app_factory("secret-token")) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


async def test_mcp_route_rejects_a_wrong_token(app_factory) -> None:
    async with _client(app_factory("right-token")) as client:
        response = await client.post(
            "/mcp", headers={"Authorization": "Bearer wrong-token"}, json={}
        )
    assert response.status_code == 401


async def test_mcp_route_rejects_a_missing_token(app_factory) -> None:
    async with _client(app_factory("right-token")) as client:
        response = await client.post("/mcp", json={})
    assert response.status_code == 401


async def test_mcp_route_accepts_the_right_token(app_factory, synced: Path) -> None:
    app = app_factory("right-token")
    async with mcp_session(app, headers={"Authorization": "Bearer right-token"}) as session:
        tools = (await session.list_tools()).tools
        assert [tool.name for tool in tools] == ["vinyl_status"]
        result = await session.call_tool("vinyl_status", {})
        assert result.is_error is not True
        assert result.structured_content["records"] == 10
        assert result.structured_content["last_result"] == "ok"


async def test_status_tool_before_any_sync(app_factory) -> None:
    app = app_factory(None)
    async with mcp_session(app) as session:
        result = await session.call_tool("vinyl_status", {})
        assert result.structured_content == {
            "records": 0,
            "bundle_generated_at": None,
            "last_sync": None,
            "last_result": None,
        }


async def test_the_ui_the_bundle_the_art_and_status_stay_open_with_a_token(
    app_factory, synced: Path
) -> None:
    async with _client(app_factory("right-token")) as client:
        page = await client.get("/")
        index = await client.get("/bundle/index.json")
        detail = await client.get("/bundle/detail/7823049.json")
        art = await client.get("/art/7823049-thumb.jpg")
        status = await client.get("/api/status")
        mcp = await client.post("/mcp", json={})

    assert page.status_code == 200
    assert "<title>Records</title>" in page.text
    assert index.status_code == 200
    assert index.json()["count"] == 10
    assert detail.status_code == 200
    assert detail.json()["section"] == "D"
    assert art.status_code == 200
    assert art.content.startswith(b"\xff\xd8")
    assert status.status_code == 200
    assert status.json()["records"] == 10
    assert mcp.status_code == 401


async def test_the_ui_assets_are_served(app_factory) -> None:
    async with _client(app_factory(None)) as client:
        css = await client.get("/app.css")
        js = await client.get("/app.js")
    assert css.status_code == 200
    assert "carousel" in css.text
    assert js.status_code == 200
    assert "bundle/index.json" in js.text


async def test_bundle_and_art_404_before_the_first_sync(app_factory) -> None:
    async with _client(app_factory(None)) as client:
        index = await client.get("/bundle/index.json")
        art = await client.get("/art/1.jpg")
    assert index.status_code == 404
    assert art.status_code == 404


async def test_a_missing_static_dir_means_no_ui_not_a_crash(app_factory, tmp_path: Path) -> None:
    app = app_factory(None, VINYL_STATIC_DIR=str(tmp_path / "nowhere"))
    async with _client(app) as client:
        page = await client.get("/")
        health = await client.get("/healthz")
    assert page.status_code == 404
    assert health.status_code == 200


async def test_the_lifespan_starts_the_nightly_loop_only_with_a_schedule_and_a_token(
    app_factory, monkeypatch
) -> None:
    started: list[tuple[str, object]] = []

    async def fake_run_daily(sync_time: str, job, **kwargs) -> int:
        started.append((sync_time, job))
        return 0

    monkeypatch.setattr(server, "run_daily", fake_run_daily)

    app = app_factory(None, VINYL_SYNC_TIME="04:30", DISCOGS_TOKEN="secret")
    async with mcp_session(app):
        pass
    assert started == [("04:30", server._scheduled_sync)]

    app = app_factory(None, VINYL_SYNC_TIME="04:30")
    async with mcp_session(app):
        pass
    assert len(started) == 1, "no token, no loop"


def test_scheduled_sync_calls_sync_once_with_the_settings(app_factory, monkeypatch) -> None:
    from joshua_vinyl import cli

    seen = []
    monkeypatch.setattr(cli, "sync_once", seen.append)
    app_factory(None)
    server._scheduled_sync()
    assert seen == [server._current_settings()]


def test_settings_are_unavailable_before_build_app(monkeypatch) -> None:
    monkeypatch.setattr(server, "_settings", None)
    with pytest.raises(RuntimeError):
        server._current_settings()
