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
        "vinyl_add",
        "vinyl_corrections",
        "vinyl_details",
        "vinyl_label_images",
        "vinyl_lend",
        "vinyl_lent_out",
        "vinyl_lookup",
        "vinyl_owned",
        "vinyl_pick",
        "vinyl_recent",
        "vinyl_remove",
        "vinyl_return",
        "vinyl_search",
        "vinyl_set_genre",
        "vinyl_set_section",
        "vinyl_set_sort_name",
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


class _FakeMB:
    def close(self) -> None:
        return None

    def search_artist(self, name: str):
        return None


@pytest.fixture
def write_app(app_factory, monkeypatch, data_dir: Path, fake_write_discogs):
    """An app whose Discogs client is the fake, with a token configured."""
    from joshua_vinyl import server as server_module

    monkeypatch.setattr(server_module, "DiscogsClient", lambda *a, **k: fake_write_discogs)
    monkeypatch.setattr(server_module, "MusicBrainzClient", lambda *a, **k: _FakeMB())
    return app_factory(
        None, DISCOGS_TOKEN="a-token", DISCOGS_USERNAME="example-user", VINYL_SYNC_TIME=""
    )


async def test_owned_tool_finds_a_record_on_the_shelf(app_factory, synced: Path) -> None:
    app = app_factory(None)
    async with mcp_session(app) as session:
        found = (
            await session.call_tool("vinyl_search", {"query": "abbey road"})
        ).structured_content["records"][0]
        result = await session.call_tool(
            "vinyl_owned", {"artist": "The Beatles", "title": found["title"]}
        )
    assert result.structured_content["owned"] is True
    assert result.structured_content["copies"][0]["section"] == "B"


async def test_owned_tool_says_no_for_a_record_the_house_lacks(app_factory, synced: Path) -> None:
    app = app_factory(None)
    async with mcp_session(app) as session:
        result = await session.call_tool(
            "vinyl_owned", {"artist": "Miles Davis", "title": "Kind Of Blue"}
        )
    assert result.structured_content["owned"] is False


async def test_owned_tool_needs_no_discogs_token(app_factory, synced: Path) -> None:
    app = app_factory(None, DISCOGS_TOKEN="")
    async with mcp_session(app) as session:
        result = await session.call_tool("vinyl_owned", {"title": "Abbey Road"})
    assert result.is_error is not True


async def test_a_lookup_without_a_token_says_which_settings_are_missing(
    app_factory, synced: Path
) -> None:
    app = app_factory(None)
    async with mcp_session(app) as session:
        result = await session.call_tool("vinyl_lookup", {"artist": "Bob Dylan"})
    assert result.is_error is True
    assert "DISCOGS_TOKEN" in str(result.content[0].text)


async def test_an_add_without_a_token_is_refused(app_factory, synced: Path) -> None:
    app = app_factory(None)
    async with mcp_session(app) as session:
        result = await session.call_tool("vinyl_add", {"release_id": 1, "confirm": True})
    assert result.is_error is True


async def test_the_lookup_tool_answers_with_candidates(
    write_app, synced: Path, fake_write_discogs
) -> None:
    fake_write_discogs.by("catno", [7590859])
    async with mcp_session(write_app) as session:
        result = await session.call_tool("vinyl_lookup", {"catalog_no": "EVR 108"})
    candidate = result.structured_content["candidates"][0]
    assert candidate["release_id"] == 7590859
    assert candidate["format_warnings"] == ["picture disc"]
    assert candidate["already_in_collection"] is True


async def test_the_lookup_tool_reports_a_matrix_it_cannot_settle(
    write_app, synced: Path, fake_write_discogs
) -> None:
    fake_write_discogs.by("catno", [7590859])
    async with mcp_session(write_app) as session:
        result = await session.call_tool(
            "vinyl_lookup", {"catalog_no": "EVR 108", "matrix": "ST-CTN-701881CTH"}
        )
    assert result.structured_content["matrix_read"] == "ST-CTN-701881CTH"
    assert "does not settle it" in result.structured_content["matrix_note"]


async def test_the_label_images_come_back_as_pictures(
    write_app, synced: Path, fake_write_discogs
) -> None:
    async with mcp_session(write_app) as session:
        result = await session.call_tool("vinyl_label_images", {"release_id": 33300852})
    kinds = {block.type for block in result.content}
    assert kinds == {"image"}
    assert result.content[0].mime_type == "image/jpeg"


async def test_an_add_through_the_tool_plans_first(
    write_app, synced: Path, fake_write_discogs
) -> None:
    async with mcp_session(write_app) as session:
        result = await session.call_tool("vinyl_add", {"release_id": 24194891})
    assert result.structured_content["written"] is False
    assert fake_write_discogs.added == []


async def test_lending_and_returning_need_no_discogs_token(app_factory, synced: Path) -> None:
    app = app_factory(None)
    async with mcp_session(app) as session:
        lent = await session.call_tool("vinyl_lend", {"release_id": 7590859, "to": "a neighbour"})
        listed = await session.call_tool("vinyl_lent_out", {})
        picked = await session.call_tool("vinyl_pick", {"section": "C"})
        back = await session.call_tool("vinyl_return", {"release_id": 7590859})
    assert lent.structured_content["lent"] is True
    assert listed.structured_content["count"] == 1
    assert listed.structured_content["records"][0]["to"] == "a neighbour"
    assert picked.structured_content["record"] is None or (
        picked.structured_content["record"]["id"] != 7590859
    )
    assert back.structured_content["returned"] is True


async def test_the_same_record_is_not_suggested_twice(app_factory, synced: Path) -> None:
    app = app_factory(None)
    async with mcp_session(app) as session:
        first = await session.call_tool("vinyl_pick", {})
        second = await session.call_tool("vinyl_pick", {})
    assert first.structured_content["record"]["id"] != second.structured_content["record"]["id"]


async def test_a_short_memory_lets_a_record_come_up_again(app_factory, synced: Path) -> None:
    app = app_factory(None)
    async with mcp_session(app) as session:
        for _ in range(3):
            result = await session.call_tool("vinyl_pick", {"section": "B", "avoid_days": 0})
    assert result.structured_content["record"] is not None


async def test_a_removal_through_the_tool_plans_first(
    write_app, synced: Path, fake_write_discogs
) -> None:
    async with mcp_session(write_app) as session:
        result = await session.call_tool("vinyl_remove", {"release_id": 7590859})
    assert result.structured_content["written"] is False
    assert fake_write_discogs.removed == []


async def test_a_confirmed_removal_through_the_tool_writes(
    write_app, synced: Path, fake_write_discogs
) -> None:
    async with mcp_session(write_app) as session:
        result = await session.call_tool("vinyl_remove", {"release_id": 7590859, "confirm": True})
    assert result.structured_content["written"] is True
    assert fake_write_discogs.removed[0][0] == 7590859


async def test_a_correction_through_the_tool_moves_the_record(app_factory, synced: Path) -> None:
    app = app_factory(None)
    async with mcp_session(app) as session:
        before = await session.call_tool("vinyl_search", {"query": "goldberg"})
        was = before.structured_content["records"][0]["section"]
        result = await session.call_tool(
            "vinyl_set_sort_name",
            {"artist": "Glenn Gould", "sort_name": "Bach, Johann Sebastian"},
        )
        after = await session.call_tool("vinyl_search", {"query": "goldberg"})
        listed = await session.call_tool("vinyl_corrections", {})
    assert result.structured_content["action"] == "set"
    assert after.structured_content["records"][0]["section"] == "B"
    assert was != "B"
    assert listed.structured_content["sort_names"]["Glenn Gould"] == "Bach, Johann Sebastian"


async def test_a_section_correction_needs_no_discogs_token(app_factory, synced: Path) -> None:
    app = app_factory(None, DISCOGS_TOKEN="")
    async with mcp_session(app) as session:
        found = await session.call_tool("vinyl_search", {"query": "nuggets"})
        record_id = found.structured_content["records"][0]["id"]
        result = await session.call_tool(
            "vinyl_set_section", {"record_id": record_id, "section": "N"}
        )
    assert result.is_error is not True
    assert result.structured_content["moved"][0]["section"] == "N"


async def test_a_genre_correction_is_dropped_by_leaving_it_out(app_factory, synced: Path) -> None:
    app = app_factory(None)
    async with mcp_session(app) as session:
        found = await session.call_tool("vinyl_search", {"query": "nuggets"})
        record_id = found.structured_content["records"][0]["id"]
        await session.call_tool("vinyl_set_genre", {"record_id": record_id, "genre": "Jazz"})
        result = await session.call_tool("vinyl_set_genre", {"record_id": record_id})
    assert result.structured_content["action"] == "cleared"
