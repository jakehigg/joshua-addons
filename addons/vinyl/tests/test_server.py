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
        assert "vinyl_status" in [tool.name for tool in tools]
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


async def test_the_tool_list_is_the_documented_set(app_factory, synced: Path) -> None:
    app = app_factory(None)
    async with mcp_session(app) as session:
        names = sorted(tool.name for tool in (await session.list_tools()).tools)
    assert names == [
        "vinyl_details",
        "vinyl_pick",
        "vinyl_recent",
        "vinyl_search",
        "vinyl_stats",
        "vinyl_status",
    ]


async def test_search_tool_finds_a_record(app_factory, synced: Path) -> None:
    app = app_factory(None)
    async with mcp_session(app) as session:
        result = await session.call_tool("vinyl_search", {"query": "beatles"})
    assert result.is_error is not True
    assert result.structured_content["found"] == 1
    assert result.structured_content["records"][0]["section"] == "B"


async def test_record_tool_reports_one_record_in_full(app_factory, synced: Path) -> None:
    app = app_factory(None)
    async with mcp_session(app) as session:
        found = (
            await session.call_tool("vinyl_search", {"query": "abbey road"})
        ).structured_content
        record_id = found["records"][0]["id"]
        result = await session.call_tool("vinyl_details", {"record_id": record_id})
    assert result.structured_content["title"] == "Abbey Road"
    assert result.structured_content["tracks"]


async def test_record_tool_names_a_record_that_is_absent(app_factory, synced: Path) -> None:
    app = app_factory(None)
    async with mcp_session(app) as session:
        result = await session.call_tool("vinyl_details", {"record_id": 1})
    assert "id 1" in result.structured_content["error"]


async def test_stats_tool_reports_the_collection(app_factory, synced: Path) -> None:
    app = app_factory(None)
    async with mcp_session(app) as session:
        result = await session.call_tool("vinyl_stats", {})
    assert result.structured_content["records"] == 10
    assert result.structured_content["genres"]


async def test_recent_tool_lists_the_newest_first(app_factory, synced: Path) -> None:
    app = app_factory(None)
    async with mcp_session(app) as session:
        result = await session.call_tool("vinyl_recent", {"limit": 3})
    added = [record["added_at"] for record in result.structured_content["records"]]
    assert added == sorted(added, reverse=True)


async def test_pick_tool_chooses_a_record_with_a_reason(app_factory, synced: Path) -> None:
    app = app_factory(None)
    async with mcp_session(app) as session:
        result = await session.call_tool("vinyl_pick", {})
    assert result.structured_content["record"]["id"]
    assert result.structured_content["reason"]


async def test_every_read_tool_says_so_before_any_sync(app_factory) -> None:
    app = app_factory(None)
    async with mcp_session(app) as session:
        for name in ("vinyl_search", "vinyl_stats", "vinyl_recent", "vinyl_pick"):
            result = await session.call_tool(name, {})
            assert "not synced yet" in result.structured_content["error"]


async def test_the_read_tools_need_the_token_too(app_factory, synced: Path) -> None:
    async with _client(app_factory("right-token")) as client:
        response = await client.post(
            "/mcp",
            headers={"Authorization": "Bearer wrong-token"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "vinyl_search", "arguments": {}},
            },
        )
    assert response.status_code == 401
