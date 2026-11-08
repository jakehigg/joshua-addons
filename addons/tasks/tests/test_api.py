"""``/api/*``: the REST routes the web UI calls, and the SSE stream."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from joshua_tasks import api, service
from joshua_tasks.events import broadcaster
from starlette.requests import Request

BOGUS = "00000000-0000-0000-0000-000000000000"


async def test_health_alias(client) -> None:
    r = await client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert (await client.get("/healthz")).json() == {"ok": True}


async def test_project_and_task_crud_flow(client) -> None:
    r = await client.post("/api/projects", json={"name": "Hallway"})
    assert r.status_code == 201
    project = r.json()
    assert project["slug"] == "hallway"
    assert set(project) == {
        "id",
        "name",
        "slug",
        "created_at",
        "archived_at",
        "task_count",
        "done_count",
    }
    assert project["archived_at"] is None and project["task_count"] == 0
    assert project["created_at"].endswith("Z")

    r = await client.get("/api/projects")
    assert [p["slug"] for p in r.json()] == ["hallway"]

    pid = project["id"]
    r = await client.post(f"/api/projects/{pid}/tasks", json={"title": "Buy paint"})
    assert r.status_code == 201
    t1 = r.json()
    assert set(t1) == {
        "id",
        "project_id",
        "group_id",
        "title",
        "notes",
        "done",
        "done_at",
        "position",
        "created_at",
        "updated_at",
    }
    assert t1["project_id"] == pid and t1["group_id"] is None and t1["notes"] == ""
    t2 = (await client.post(f"/api/projects/{pid}/tasks", json={"title": "Tape edges"})).json()

    r = await client.get(f"/api/projects/{pid}/tasks")
    assert [t["title"] for t in r.json()] == ["Buy paint", "Tape edges"]

    r = await client.patch(f"/api/tasks/{t1['id']}", json={"done": True})
    assert r.json()["done"] is True and r.json()["done_at"] is not None
    r = await client.patch(f"/api/tasks/{t1['id']}", json={"done": False})
    assert r.json()["done"] is False and r.json()["done_at"] is None

    r = await client.put(
        f"/api/projects/{pid}/tasks/order", json={"ordered_ids": [t2["id"], t1["id"]]}
    )
    assert r.status_code == 200
    assert [t["title"] for t in r.json()] == ["Tape edges", "Buy paint"]
    assert [t["position"] for t in r.json()] == [1024.0, 2048.0]
    r = await client.get(f"/api/projects/{pid}/tasks")
    assert [t["title"] for t in r.json()] == ["Tape edges", "Buy paint"]

    # a stale reorder conflicts
    r = await client.put(f"/api/projects/{pid}/tasks/order", json={"ordered_ids": [t1["id"]]})
    assert r.status_code == 409
    assert "every task currently in this container" in r.json()["detail"]

    await client.patch(f"/api/tasks/{t1['id']}", json={"done": True})
    listing = (await client.get("/api/projects")).json()[0]
    assert listing["task_count"] == 2 and listing["done_count"] == 1

    assert (await client.delete(f"/api/tasks/{t2['id']}")).status_code == 204
    r = await client.delete(f"/api/projects/{pid}")
    assert r.status_code == 204 and r.content == b""
    assert (await client.get("/api/projects")).json() == []


async def test_task_notes_and_title_patch(client) -> None:
    pid = (await client.post("/api/projects", json={"name": "P"})).json()["id"]
    t = (await client.post(f"/api/projects/{pid}/tasks", json={"title": "a", "notes": "n"})).json()
    assert t["notes"] == "n"
    r = await client.patch(f"/api/tasks/{t['id']}", json={"title": " b ", "notes": "see https://x"})
    assert r.json()["title"] == "b" and r.json()["notes"] == "see https://x"


async def test_project_patch_rename_and_archive(client) -> None:
    p = (await client.post("/api/projects", json={"name": "Garden"})).json()
    r = await client.patch(f"/api/projects/{p['id']}", json={"name": "Yard", "archived": True})
    assert r.status_code == 200
    assert r.json()["name"] == "Yard" and r.json()["slug"] == "garden"
    assert r.json()["archived_at"] is not None
    assert (await client.get("/api/projects")).json() == []
    r = await client.patch(f"/api/projects/{p['id']}", json={"archived": False})
    assert r.json()["archived_at"] is None
    assert len((await client.get("/api/projects")).json()) == 1


async def test_group_flow(client) -> None:
    pid = (await client.post("/api/projects", json={"name": "P"})).json()["id"]
    r = await client.post(f"/api/projects/{pid}/groups", json={"name": "Prep"})
    assert r.status_code == 201
    g1 = r.json()
    assert set(g1) == {
        "id",
        "project_id",
        "name",
        "position",
        "created_at",
        "updated_at",
        "task_count",
        "done_count",
    }
    g2 = (await client.post(f"/api/projects/{pid}/groups", json={"name": "Paint"})).json()

    r = await client.post(
        f"/api/projects/{pid}/tasks", json={"title": "in group", "group_id": g1["id"]}
    )
    assert r.json()["group_id"] == g1["id"]
    tid = r.json()["id"]
    await client.patch(f"/api/tasks/{tid}", json={"done": True})

    groups = (await client.get(f"/api/projects/{pid}/groups")).json()
    assert [(g["name"], g["task_count"], g["done_count"]) for g in groups] == [
        ("Prep", 1, 1),
        ("Paint", 0, 0),
    ]

    r = await client.put(
        f"/api/projects/{pid}/groups/order", json={"ordered_ids": [g2["id"], g1["id"]]}
    )
    assert [g["name"] for g in r.json()] == ["Paint", "Prep"]
    r = await client.put(f"/api/projects/{pid}/groups/order", json={"ordered_ids": [g1["id"]]})
    assert r.status_code == 409

    r = await client.patch(f"/api/groups/{g1['id']}", json={"name": "Prepare"})
    assert r.json()["name"] == "Prepare"

    assert (await client.delete(f"/api/groups/{g1['id']}")).status_code == 204
    tasks = (await client.get(f"/api/projects/{pid}/tasks")).json()
    assert [(t["title"], t["group_id"]) for t in tasks] == [("in group", None)]


async def test_patch_task_group_is_presence_sensitive(client) -> None:
    pid = (await client.post("/api/projects", json={"name": "P"})).json()["id"]
    gid = (await client.post(f"/api/projects/{pid}/groups", json={"name": "G"})).json()["id"]
    tid = (await client.post(f"/api/projects/{pid}/tasks", json={"title": "t"})).json()["id"]

    r = await client.patch(f"/api/tasks/{tid}", json={"group_id": gid})
    assert r.json()["group_id"] == gid
    # omitted: the group is left alone
    r = await client.patch(f"/api/tasks/{tid}", json={"notes": "x"})
    assert r.json()["group_id"] == gid
    # explicit null: ungrouped
    r = await client.patch(f"/api/tasks/{tid}", json={"group_id": None})
    assert r.json()["group_id"] is None
    # a group from another project is a 404
    other = (await client.post("/api/projects", json={"name": "Other"})).json()["id"]
    foreign = (await client.post(f"/api/projects/{other}/groups", json={"name": "F"})).json()["id"]
    r = await client.patch(f"/api/tasks/{tid}", json={"group_id": foreign})
    assert r.status_code == 404


async def test_reorder_into_a_group_in_one_call(client) -> None:
    pid = (await client.post("/api/projects", json={"name": "P"})).json()["id"]
    gid = (await client.post(f"/api/projects/{pid}/groups", json={"name": "G"})).json()["id"]
    a = (await client.post(f"/api/projects/{pid}/tasks", json={"title": "a"})).json()["id"]
    b = (await client.post(f"/api/projects/{pid}/tasks", json={"title": "b"})).json()["id"]
    r = await client.put(
        f"/api/projects/{pid}/tasks/order", json={"ordered_ids": [a], "group_id": gid}
    )
    assert [t["id"] for t in r.json()] == [a]
    assert r.json()[0]["group_id"] == gid
    root = await client.put(
        f"/api/projects/{pid}/tasks/order", json={"ordered_ids": [b], "group_id": None}
    )
    assert [t["id"] for t in root.json()] == [b]


async def test_404s(client) -> None:
    assert (await client.get(f"/api/projects/{BOGUS}/tasks")).status_code == 404
    assert (await client.get(f"/api/projects/{BOGUS}/groups")).status_code == 404
    assert (
        await client.post(f"/api/projects/{BOGUS}/tasks", json={"title": "t"})
    ).status_code == 404
    assert (
        await client.post(f"/api/projects/{BOGUS}/groups", json={"name": "g"})
    ).status_code == 404
    assert (await client.patch(f"/api/projects/{BOGUS}", json={"name": "x"})).status_code == 404
    assert (await client.patch(f"/api/tasks/{BOGUS}", json={"done": True})).status_code == 404
    assert (await client.patch(f"/api/groups/{BOGUS}", json={"name": "x"})).status_code == 404
    assert (await client.delete(f"/api/projects/{BOGUS}")).status_code == 404
    assert (await client.delete(f"/api/tasks/{BOGUS}")).status_code == 404
    assert (await client.delete(f"/api/groups/{BOGUS}")).status_code == 404
    r = await client.put(f"/api/projects/{BOGUS}/tasks/order", json={"ordered_ids": []})
    assert r.status_code == 404
    r = await client.put(f"/api/projects/{BOGUS}/groups/order", json={"ordered_ids": []})
    assert r.status_code == 404
    body = (await client.get(f"/api/projects/{BOGUS}/tasks")).json()
    assert body == {"detail": f"project not found: {BOGUS}"}


async def test_validation_errors_are_422_in_the_fastapi_shape(client) -> None:
    r = await client.post("/api/projects", json={"name": ""})
    assert r.status_code == 422
    [err] = r.json()["detail"]
    assert err["loc"] == ["body", "name"] and err["type"] == "string_too_short"

    r = await client.get("/api/projects/not-a-uuid/tasks")
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["path", "project_id"]

    r = await client.post(
        "/api/projects", content=b"{nope", headers={"content-type": "application/json"}
    )
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "json_invalid"

    r = await client.post("/api/projects", json=["x"])
    assert r.status_code == 422

    pid = (await client.post("/api/projects", json={"name": "P"})).json()["id"]
    r = await client.post(f"/api/projects/{pid}/tasks", json={"title": "x" * 1001})
    assert r.status_code == 422
    r = await client.put(f"/api/projects/{pid}/tasks/order", json={"ordered_ids": ["junk"]})
    assert r.status_code == 422


async def test_unknown_routes_and_methods(client) -> None:
    assert (await client.get("/api/nope")).status_code == 404
    assert (await client.get("/api/projects/")).status_code == 404  # no slash redirect
    assert (await client.put("/api/projects")).status_code == 405


async def test_api_open_even_with_an_addon_token(bound_db, app_factory) -> None:
    import httpx

    transport = httpx.ASGITransport(app=app_factory("secret"))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        assert (await c.get("/api/projects")).status_code == 200
        assert (await c.post("/mcp", json={})).status_code == 401


# --- SSE ---------------------------------------------------------------------


def _events_request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/api/events", "headers": []})


async def _next_data(stream) -> dict:
    chunk = await asyncio.wait_for(anext(stream), timeout=5)
    assert chunk.startswith("data: ")
    return json.loads(chunk[len("data: ") :])


async def test_events_stream_headers_and_connect_comment() -> None:
    response = await api.events(_events_request())
    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    stream = response.body_iterator
    try:
        assert await anext(stream) == ": connected\n\n"
    finally:
        await stream.aclose()


async def test_events_echo_the_client_id_as_origin(client) -> None:
    response = await api.events(_events_request())
    stream = response.body_iterator
    try:
        assert await anext(stream) == ": connected\n\n"
        r = await client.post(
            "/api/projects", json={"name": "Live"}, headers={"X-Client-Id": "tab-1"}
        )
        assert await _next_data(stream) == {
            "scope": "projects",
            "project_id": None,
            "origin": "tab-1",
        }
        pid = r.json()["id"]

        await client.post(f"/api/projects/{pid}/tasks", json={"title": "t"})  # no header
        assert await _next_data(stream) == {"scope": "tasks", "project_id": pid, "origin": None}

        await client.post(
            f"/api/projects/{pid}/groups", json={"name": "G"}, headers={"X-Client-Id": "tab-2"}
        )
        assert (await _next_data(stream))["origin"] == "tab-2"
    finally:
        await stream.aclose()


async def test_service_calls_outside_the_api_publish_origin_null(client, sessionmaker_) -> None:
    """What an MCP tool call does: no request header, so origin is null."""
    response = await api.events(_events_request())
    stream = response.body_iterator
    try:
        await anext(stream)
        async with sessionmaker_() as session:
            await service.create_project(session, "From Joshua")
        assert (await _next_data(stream))["origin"] is None
    finally:
        await stream.aclose()


async def test_origin_does_not_leak_between_requests(client) -> None:
    response = await api.events(_events_request())
    stream = response.body_iterator
    try:
        await anext(stream)
        await client.post("/api/projects", json={"name": "A"}, headers={"X-Client-Id": "tab-1"})
        await _next_data(stream)
        await client.post("/api/projects", json={"name": "B"})
        assert (await _next_data(stream))["origin"] is None
    finally:
        await stream.aclose()


async def test_events_keepalive_ping_and_unsubscribe(monkeypatch) -> None:
    monkeypatch.setattr(api, "KEEPALIVE_SECONDS", 0.01)
    before = len(broadcaster._subscribers)
    response = await api.events(_events_request())
    stream = response.body_iterator
    assert await anext(stream) == ": connected\n\n"
    assert await anext(stream) == ": ping\n\n"
    assert len(broadcaster._subscribers) == before + 1
    await stream.aclose()
    assert len(broadcaster._subscribers) == before


def test_keepalive_is_25_seconds() -> None:
    assert api.KEEPALIVE_SECONDS == 25.0


async def test_event_origin_middleware_passes_non_http_scopes_through() -> None:
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    await api.EventOriginMiddleware(inner)({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]


async def test_new_ids_are_valid_uuids(client) -> None:
    r = await client.post("/api/projects", json={"name": "U"})
    assert str(uuid.UUID(r.json()["id"])) == r.json()["id"]


@pytest.mark.parametrize("path", ["/api/projects", "/api/health"])
async def test_get_routes_answer_json(client, path) -> None:
    r = await client.get(path)
    assert r.headers["content-type"].startswith("application/json")
