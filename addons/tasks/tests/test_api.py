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
        "goal",
        "target_date",
        "status",
        "completed_at",
        "created_at",
        "archived_at",
        "task_count",
        "done_count",
        "open_count",
        "percent",
        "health",
        "last_progress_at",
        "next_task",
    }
    assert project["status"] == "active" and project["goal"] is None
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
        "size",
        "due_date",
        "assignee",
        "position",
        "created_at",
        "updated_at",
        "blocked",
        "blockers",
        "blocking",
    }
    assert t1["project_id"] == pid and t1["group_id"] is None and t1["notes"] == ""
    assert t1["blocked"] is False and t1["blockers"] == [] and t1["blocking"] == []
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
        "target_date",
        "created_at",
        "updated_at",
        "task_count",
        "done_count",
        "percent",
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


# --- goals, status, and next steps (issue #37) ---------------------------------


async def test_project_goal_status_and_percent(client) -> None:
    r = await client.post(
        "/api/projects", json={"name": "Hallway", "goal": "Painted", "target_date": "2030-01-31"}
    )
    assert r.status_code == 201
    p = r.json()
    assert (p["goal"], p["target_date"], p["health"]) == ("Painted", "2030-01-31", None)
    pid = p["id"]
    t1 = (await client.post(f"/api/projects/{pid}/tasks", json={"title": "a"})).json()
    await client.post(f"/api/projects/{pid}/tasks", json={"title": "b"})
    await client.patch(f"/api/tasks/{t1['id']}", json={"done": True})

    [listed] = (await client.get("/api/projects")).json()
    assert (listed["percent"], listed["open_count"], listed["health"]) == (50, 1, "on_track")
    assert listed["next_task"]["title"] == "b"
    assert listed["last_progress_at"] is not None

    # goal is presence-sensitive: a rename keeps it, an explicit null clears it
    r = await client.patch(f"/api/projects/{pid}", json={"name": "Hall"})
    assert r.json()["goal"] == "Painted" and r.json()["percent"] == 50
    r = await client.patch(f"/api/projects/{pid}", json={"goal": None, "target_date": None})
    assert r.json()["goal"] is None and r.json()["target_date"] is None

    # done is refused while a task is open; closed is not
    r = await client.patch(f"/api/projects/{pid}", json={"status": "done"})
    assert r.status_code == 409 and "still open" in r.json()["detail"]
    r = await client.patch(f"/api/projects/{pid}", json={"status": "closed"})
    assert r.json()["status"] == "closed" and r.json()["completed_at"] is not None
    r = await client.patch(f"/api/projects/{pid}", json={"status": "active"})
    assert r.json()["status"] == "active" and r.json()["completed_at"] is None
    r = await client.patch(f"/api/projects/{pid}", json={"status": "sideways"})
    assert r.status_code == 422


async def test_next_steps_endpoint(client) -> None:
    pid = (await client.post("/api/projects", json={"name": "P"})).json()["id"]
    await client.post(f"/api/projects/{pid}/tasks", json={"title": "backlog"})
    g = (await client.post(f"/api/projects/{pid}/groups", json={"name": "Phase 1"})).json()
    await client.post(f"/api/projects/{pid}/tasks", json={"title": "first", "group_id": g["id"]})
    await client.post("/api/projects", json={"name": "Quiet"})

    r = await client.get("/api/next-steps")
    assert r.status_code == 200
    [step] = r.json()
    assert step["slug"] == "p" and [t["title"] for t in step["tasks"]] == ["first", "backlog"]
    r = await client.get("/api/next-steps", params={"project": "quiet", "limit": "1"})
    [quiet] = r.json()
    assert quiet["slug"] == "quiet" and quiet["tasks"] == []
    r = await client.get("/api/next-steps", params={"limit": "lots"})
    assert r.status_code == 422
    r = await client.get("/api/next-steps", params={"project": "nope"})
    assert r.status_code == 404


async def test_group_target_date_is_presence_sensitive(client) -> None:
    pid = (await client.post("/api/projects", json={"name": "P"})).json()["id"]
    g = (await client.post(f"/api/projects/{pid}/groups", json={"name": "Phase 1"})).json()
    r = await client.patch(f"/api/groups/{g['id']}", json={"target_date": "2030-03-01"})
    assert r.json()["target_date"] == "2030-03-01"
    r = await client.patch(f"/api/groups/{g['id']}", json={"name": "Phase one"})
    assert r.json()["target_date"] == "2030-03-01" and r.json()["name"] == "Phase one"
    r = await client.patch(f"/api/groups/{g['id']}", json={"target_date": None})
    assert r.json()["target_date"] is None


async def test_task_size_due_date_and_assignee_over_rest(client) -> None:
    pid = (await client.post("/api/projects", json={"name": "P"})).json()["id"]
    r = await client.post(
        f"/api/projects/{pid}/tasks",
        json={"title": "t", "size": "L", "due_date": "2030-02-02", "assignee": "Sam"},
    )
    assert r.status_code == 201
    t = r.json()
    assert (t["size"], t["due_date"], t["assignee"]) == ("L", "2030-02-02", "Sam")
    r = await client.patch(f"/api/tasks/{t['id']}", json={"title": "renamed"})
    assert r.json()["size"] == "L" and r.json()["assignee"] == "Sam"
    r = await client.patch(f"/api/tasks/{t['id']}", json={"size": None, "assignee": ""})
    assert r.json()["size"] is None and r.json()["assignee"] is None
    r = await client.post(f"/api/projects/{pid}/tasks", json={"title": "x", "size": "huge"})
    assert r.status_code == 422
    r = await client.patch(f"/api/tasks/{t['id']}", json={"due_date": "soon"})
    assert r.status_code == 422


# --- search (issue #40) --------------------------------------------------------


async def test_search_endpoint(client) -> None:
    hall = (
        await client.post("/api/projects", json={"name": "Hallway", "goal": "Hallway painted"})
    ).json()
    garden = (await client.post("/api/projects", json={"name": "Garden"})).json()
    g = (await client.post(f"/api/projects/{hall['id']}/groups", json={"name": "Prep"})).json()
    await client.post(
        f"/api/projects/{hall['id']}/tasks", json={"title": "Tape edges", "notes": "no paint here"}
    )
    buy = (
        await client.post(
            f"/api/projects/{hall['id']}/tasks", json={"title": "Buy PAINT", "group_id": g["id"]}
        )
    ).json()
    await client.post(f"/api/projects/{garden['id']}/tasks", json={"title": "Dig"})

    r = await client.get("/api/search", params={"query": "paint"})
    assert r.status_code == 200
    found = r.json()
    assert [p["slug"] for p in found["projects"]] == ["hallway"]
    assert found["projects"][0]["open_count"] == 2
    assert [f["task"]["title"] for f in found["tasks"]] == ["Buy PAINT", "Tape edges"]
    first = found["tasks"][0]
    assert first["task"]["id"] == buy["id"] and first["project"]["slug"] == "hallway"
    assert first["group"]["name"] == "Prep" and first["group"]["id"] == g["id"]
    assert found["tasks"][1]["group"] is None

    # one project only, and the limit
    r = await client.get("/api/search", params={"query": "paint", "project": "hallway", "limit": 1})
    assert [f["task"]["title"] for f in r.json()["tasks"]] == ["Buy PAINT"]

    # the open-only default, and include_done
    await client.patch(f"/api/tasks/{buy['id']}", json={"done": True})
    r = await client.get("/api/search", params={"query": "paint"})
    assert [f["task"]["title"] for f in r.json()["tasks"]] == ["Tape edges"]
    r = await client.get("/api/search", params={"query": "paint", "include_done": "true"})
    assert [f["task"]["title"] for f in r.json()["tasks"]] == ["Buy PAINT", "Tape edges"]

    # an archived project is never searched
    await client.patch(f"/api/projects/{hall['id']}", json={"archived": True})
    r = await client.get("/api/search", params={"query": "paint", "include_done": "1"})
    assert r.json() == {"projects": [], "tasks": []}

    r = await client.get("/api/search", params={"query": "zebra"})
    assert r.json() == {"projects": [], "tasks": []}
    assert (await client.get("/api/search")).json() == {"projects": [], "tasks": []}


async def test_search_endpoint_bad_parameters(client) -> None:
    r = await client.get("/api/search", params={"query": "x", "limit": "lots"})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["query", "limit"]
    r = await client.get("/api/search", params={"query": "x", "include_done": "maybe"})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["query", "include_done"]
    r = await client.get("/api/search", params={"query": "x", "project": "nope"})
    assert r.status_code == 404


# --- blocking (issue #52) ------------------------------------------------------


async def test_blocker_endpoints_over_rest(client) -> None:
    hall = (
        await client.post("/api/projects", json={"name": "Hallway", "goal": "Hallway painted"})
    ).json()
    garage = (await client.post("/api/projects", json={"name": "Garage"})).json()
    trim = (
        await client.post(f"/api/projects/{hall['id']}/tasks", json={"title": "Paint the trim"})
    ).json()
    keys = (
        await client.post(f"/api/projects/{garage['id']}/tasks", json={"title": "Collect the keys"})
    ).json()

    r = await client.post(f"/api/tasks/{trim['id']}/blockers", json={"blocker_id": keys["id"]})
    assert r.status_code == 201
    blocked = r.json()
    assert blocked["blocked"] is True
    assert [b["title"] for b in blocked["blockers"]] == ["Collect the keys"]
    assert blocked["blockers"][0]["project"] == {"name": "Garage", "slug": "garage"}

    # the other side reads the link as well, and a blocked task is not next
    [other] = (await client.get(f"/api/projects/{garage['id']}/tasks")).json()
    assert other["blocked"] is False
    assert [b["title"] for b in other["blocking"]] == ["Paint the trim"]
    projects = (await client.get("/api/projects")).json()
    [summary] = [p for p in projects if p["slug"] == "hallway"]
    assert summary["next_task"] is None and summary["health"] == "stalled"

    # the last blocker going done unblocks the task
    await client.patch(f"/api/tasks/{keys['id']}", json={"done": True})
    [waiting] = (await client.get(f"/api/projects/{hall['id']}/tasks")).json()
    assert waiting["blocked"] is False

    r = await client.delete(f"/api/tasks/{trim['id']}/blockers/{keys['id']}")
    assert r.status_code == 200 and r.json()["blockers"] == []
    # a link that is not there is no error
    again = await client.delete(f"/api/tasks/{trim['id']}/blockers/{keys['id']}")
    assert again.status_code == 200


async def test_blocker_endpoints_refuse_a_loop_and_a_bad_id(client) -> None:
    pid = (await client.post("/api/projects", json={"name": "P"})).json()["id"]
    a = (await client.post(f"/api/projects/{pid}/tasks", json={"title": "a"})).json()
    b = (await client.post(f"/api/projects/{pid}/tasks", json={"title": "b"})).json()
    assert (
        await client.post(f"/api/tasks/{b['id']}/blockers", json={"blocker_id": a["id"]})
    ).status_code == 201

    r = await client.post(f"/api/tasks/{a['id']}/blockers", json={"blocker_id": b["id"]})
    assert r.status_code == 409
    assert r.json()["detail"] == "this link makes a loop: a blocks b blocks a"

    r = await client.post(f"/api/tasks/{a['id']}/blockers", json={"blocker_id": a["id"]})
    assert r.status_code == 409 and "cannot block itself" in r.json()["detail"]

    r = await client.post(f"/api/tasks/{a['id']}/blockers", json={"blocker_id": str(uuid.uuid4())})
    assert r.status_code == 404
    r = await client.post(f"/api/tasks/{a['id']}/blockers", json={"blocker_id": "nope"})
    assert r.status_code == 422
    r = await client.delete(f"/api/tasks/nope/blockers/{a['id']}")
    assert r.status_code == 422
