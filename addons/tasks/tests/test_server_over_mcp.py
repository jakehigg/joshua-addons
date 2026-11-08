"""Every tool over a real in-process MCP session, plus the exact tool list."""

from __future__ import annotations

import uuid

import pytest
from conftest import mcp_session

TOOL_NAMES = {
    "list_projects",
    "create_project",
    "rename_project",
    "archive_project",
    "list_tasks",
    "create_task",
    "update_task",
    "set_task_done",
    "reorder_tasks",
    "move_task",
    "create_group",
    "rename_group",
    "delete_group",
    "set_task_group",
    "set_project_goal",
    "complete_project",
    "set_project_status",
    "next_steps",
    "set_group_target_date",
    "search_tasks",
}


def with_session(fn):
    """Run ``fn(session)`` inside one in-process MCP session on a fresh database.

    A fixture cannot own the session: the MCP client's task groups must be
    entered and exited in the same task, which a generator fixture does not
    guarantee.
    """

    async def wrapper(app_factory) -> None:
        async with mcp_session(app_factory(None)) as session:
            await fn(session)

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


async def call(session, name: str, args: dict | None = None) -> dict:
    result = await session.call_tool(name, args or {})
    assert result.is_error is not True, result.content
    return result.structured_content


async def call_error(session, name: str, args: dict) -> str:
    result = await session.call_tool(name, args)
    assert result.is_error is True
    [block] = result.content
    assert "Traceback" not in block.text
    return block.text


@with_session
async def test_tool_names_are_exactly_the_twenty(session) -> None:
    names = [t.name for t in (await session.list_tools()).tools]
    assert len(names) == 20
    assert set(names) == TOOL_NAMES


@with_session
async def test_tools_declare_their_arguments(session) -> None:
    tools = {t.name: t.input_schema for t in (await session.list_tools()).tools}
    props = {
        n: (set(s.get("properties", {})), set(s.get("required", []))) for n, s in tools.items()
    }
    assert props == {
        "list_projects": ({"include_finished"}, set()),
        "create_project": ({"name", "goal", "target_date"}, {"name"}),
        "rename_project": ({"project", "name"}, {"project", "name"}),
        "archive_project": ({"project"}, {"project"}),
        "list_tasks": ({"project", "include_done"}, {"project"}),
        "create_task": (
            {"project", "title", "notes", "group", "size", "due_date", "assignee"},
            {"project", "title"},
        ),
        "update_task": (
            {"task_id", "title", "notes", "size", "due_date", "assignee"},
            {"task_id"},
        ),
        "set_task_done": ({"task_id", "done"}, {"task_id", "done"}),
        "reorder_tasks": (
            {"project", "ordered_task_ids", "group"},
            {"project", "ordered_task_ids"},
        ),
        "move_task": ({"task_id", "to_project"}, {"task_id", "to_project"}),
        "create_group": ({"project", "name"}, {"project", "name"}),
        "rename_group": ({"group_id", "name"}, {"group_id", "name"}),
        "delete_group": ({"group_id"}, {"group_id"}),
        "set_task_group": ({"task_id", "group"}, {"task_id"}),
        "set_project_goal": ({"project", "goal", "target_date"}, {"project", "goal"}),
        "complete_project": ({"project"}, {"project"}),
        "set_project_status": ({"project", "status"}, {"project", "status"}),
        "next_steps": ({"project", "limit"}, set()),
        "set_group_target_date": ({"group_id", "target_date"}, {"group_id", "target_date"}),
        "search_tasks": ({"query", "include_done", "project", "limit"}, {"query"}),
    }


@with_session
async def test_create_project_and_list_projects(session) -> None:
    created = await call(session, "create_project", {"name": "Paint the hallway"})
    assert created["slug"] == "paint-the-hallway" and created["archived"] is False
    uuid.UUID(created["id"])
    listing = await call(session, "list_projects")
    assert listing == {
        "result": [
            {
                "id": created["id"],
                "name": "Paint the hallway",
                "slug": "paint-the-hallway",
                "goal": None,
                "target_date": None,
                "status": "active",
                "task_count": 0,
                "done_count": 0,
                "open_count": 0,
                "percent": 0,
                "health": None,
                "next_task": None,
            }
        ]
    }


@with_session
async def test_rename_project_keeps_slug(session) -> None:
    await call(session, "create_project", {"name": "Garden"})
    renamed = await call(session, "rename_project", {"project": "garden", "name": "Yard"})
    assert renamed["name"] == "Yard" and renamed["slug"] == "garden"


@with_session
async def test_archive_project_hides_it(session) -> None:
    await call(session, "create_project", {"name": "Old"})
    result = await call(session, "archive_project", {"project": "old"})
    assert result["ok"] is True and result["project"]["archived"] is True
    assert (await call(session, "list_projects")) == {"result": []}


@with_session
async def test_create_task_and_list_tasks(session) -> None:
    await call(session, "create_project", {"name": "P"})
    task = await call(session, "create_task", {"project": "p", "title": "Buy paint", "notes": "n"})
    assert task["title"] == "Buy paint" and task["notes"] == "n"
    assert task["done"] is False and task["group_id"] is None and task["position"] == 1024.0
    await call(session, "create_task", {"project": "p", "title": "Tape"})
    listing = await call(session, "list_tasks", {"project": "p"})
    assert [t["title"] for t in listing["backlog"]] == ["Buy paint", "Tape"]
    assert listing["groups"] == []


@with_session
async def test_list_tasks_include_done_false_and_group_counts(session) -> None:
    await call(session, "create_project", {"name": "P"})
    await call(session, "create_group", {"project": "p", "name": "G"})
    a = await call(session, "create_task", {"project": "p", "title": "a", "group": "G"})
    await call(session, "create_task", {"project": "p", "title": "b", "group": "g"})
    await call(session, "create_task", {"project": "p", "title": "loose"})
    await call(session, "set_task_done", {"task_id": a["id"], "done": True})
    listing = await call(session, "list_tasks", {"project": "p", "include_done": False})
    [group] = listing["groups"]
    assert (group["task_count"], group["done_count"]) == (2, 1)
    assert [t["title"] for t in group["tasks"]] == ["b"]
    assert [t["title"] for t in listing["backlog"]] == ["loose"]


@with_session
async def test_update_task(session) -> None:
    await call(session, "create_project", {"name": "P"})
    t = await call(session, "create_task", {"project": "p", "title": "old"})
    updated = await call(session, "update_task", {"task_id": t["id"], "title": "new", "notes": "x"})
    assert updated["title"] == "new" and updated["notes"] == "x" and updated["done"] is False


@with_session
async def test_set_task_done_and_back(session) -> None:
    await call(session, "create_project", {"name": "P"})
    t = await call(session, "create_task", {"project": "p", "title": "t"})
    done = await call(session, "set_task_done", {"task_id": t["id"], "done": True})
    assert done["done"] is True
    reopened = await call(session, "set_task_done", {"task_id": t["id"], "done": False})
    assert reopened["done"] is False


@with_session
async def test_reorder_tasks_and_stale_error(session) -> None:
    await call(session, "create_project", {"name": "P"})
    a = await call(session, "create_task", {"project": "p", "title": "a"})
    b = await call(session, "create_task", {"project": "p", "title": "b"})
    result = await call(
        session, "reorder_tasks", {"project": "p", "ordered_task_ids": [b["id"], a["id"]]}
    )
    assert result == {"ok": True, "order": [b["id"], a["id"]]}
    listing = await call(session, "list_tasks", {"project": "p"})
    assert [t["title"] for t in listing["backlog"]] == ["b", "a"]
    message = await call_error(
        session, "reorder_tasks", {"project": "p", "ordered_task_ids": [a["id"]]}
    )
    assert "every task currently in this container" in message


@with_session
async def test_reorder_tasks_into_a_group(session) -> None:
    await call(session, "create_project", {"name": "P"})
    await call(session, "create_group", {"project": "p", "name": "G"})
    a = await call(session, "create_task", {"project": "p", "title": "a"})
    result = await call(
        session, "reorder_tasks", {"project": "p", "ordered_task_ids": [a["id"]], "group": "G"}
    )
    assert result["order"] == [a["id"]]
    listing = await call(session, "list_tasks", {"project": "p"})
    assert listing["backlog"] == []
    assert [t["title"] for t in listing["groups"][0]["tasks"]] == ["a"]


@with_session
async def test_move_task_to_another_project(session) -> None:
    await call(session, "create_project", {"name": "Src"})
    await call(session, "create_project", {"name": "Dst"})
    t = await call(session, "create_task", {"project": "src", "title": "roaming"})
    moved = await call(session, "move_task", {"task_id": t["id"], "to_project": "dst"})
    assert moved["id"] == t["id"]
    assert (await call(session, "list_tasks", {"project": "src"}))["backlog"] == []
    assert [
        x["title"] for x in (await call(session, "list_tasks", {"project": "dst"}))["backlog"]
    ] == ["roaming"]


@with_session
async def test_create_group(session) -> None:
    await call(session, "create_project", {"name": "P"})
    group = await call(session, "create_group", {"project": "p", "name": "Prep work"})
    assert group["name"] == "Prep work" and group["position"] == 1024.0
    uuid.UUID(group["id"])


@with_session
async def test_rename_group(session) -> None:
    await call(session, "create_project", {"name": "P"})
    g = await call(session, "create_group", {"project": "p", "name": "Old"})
    renamed = await call(session, "rename_group", {"group_id": g["id"], "name": "New"})
    assert renamed == {"id": g["id"], "name": "New", "position": 1024.0, "target_date": None}


@with_session
async def test_delete_group_ungroups_tasks(session) -> None:
    await call(session, "create_project", {"name": "P"})
    g = await call(session, "create_group", {"project": "p", "name": "G"})
    await call(session, "create_task", {"project": "p", "title": "a", "group": g["id"]})
    assert await call(session, "delete_group", {"group_id": g["id"]}) == {"ok": True}
    listing = await call(session, "list_tasks", {"project": "p"})
    assert listing["groups"] == []
    assert [t["title"] for t in listing["backlog"]] == ["a"]


@with_session
async def test_set_task_group_in_and_out(session) -> None:
    await call(session, "create_project", {"name": "P"})
    g = await call(session, "create_group", {"project": "p", "name": "G"})
    t = await call(session, "create_task", {"project": "p", "title": "a"})
    inside = await call(session, "set_task_group", {"task_id": t["id"], "group": "g"})
    assert inside["group_id"] == g["id"]
    outside = await call(session, "set_task_group", {"task_id": t["id"]})
    assert outside["group_id"] is None


@with_session
async def test_unknown_project_error_is_clean(session) -> None:
    message = await call_error(session, "list_tasks", {"project": "nope"})
    assert "project not found: nope" in message
    assert "project not found" in await call_error(
        session, "rename_project", {"project": "nope", "name": "x"}
    )
    assert "project not found" in await call_error(session, "archive_project", {"project": "nope"})


@with_session
async def test_bad_and_missing_ids_are_clean_errors(session) -> None:
    assert "task_id is not a valid id: junk" in await call_error(
        session, "set_task_done", {"task_id": "junk", "done": True}
    )
    missing = str(uuid.uuid4())
    assert "task not found" in await call_error(
        session, "update_task", {"task_id": missing, "title": "x"}
    )
    assert "group not found" in await call_error(session, "delete_group", {"group_id": missing})
    assert "not a valid id" in await call_error(
        session, "rename_group", {"group_id": "junk", "name": "x"}
    )
    assert "not a valid id" in await call_error(session, "set_task_group", {"task_id": "junk"})
    assert "not a valid id" in await call_error(session, "update_task", {"task_id": "junk"})
    assert "not a valid id" in await call_error(
        session, "move_task", {"task_id": "junk", "to_project": "p"}
    )
    assert "not a valid id" in await call_error(
        session, "reorder_tasks", {"project": "p", "ordered_task_ids": ["junk"]}
    )
    assert "not a valid id" in await call_error(session, "delete_group", {"group_id": "junk"})


@with_session
async def test_ambiguous_group_and_missing_group_errors(session) -> None:
    await call(session, "create_project", {"name": "P"})
    await call(session, "create_group", {"project": "p", "name": "Same"})
    await call(session, "create_group", {"project": "p", "name": "same"})
    assert "ambiguous" in await call_error(
        session, "create_task", {"project": "p", "title": "t", "group": "SAME"}
    )
    assert "group not found" in await call_error(
        session, "create_task", {"project": "p", "title": "t", "group": "zzz"}
    )


@with_session
async def test_project_resolves_by_name_too(session) -> None:
    await call(session, "create_project", {"name": "Paint the Hallway"})
    task = await call(session, "create_task", {"project": "paint the hallway", "title": "t"})
    assert task["title"] == "t"


async def test_session_factory_is_unset_outside_the_lifespan() -> None:
    from joshua_tasks import server

    with pytest.raises(RuntimeError, match="not ready"):
        server._session_factory()


# --- goals, status, and next steps (issue #37) ---------------------------------


@with_session
async def test_create_project_with_goal_and_list_shows_progress(session) -> None:
    created = await call(
        session,
        "create_project",
        {"name": "Hallway", "goal": "Hallway painted", "target_date": "2030-01-31"},
    )
    assert created["goal"] == "Hallway painted" and created["target_date"] == "2030-01-31"
    assert created["status"] == "active"
    a = await call(session, "create_task", {"project": "hallway", "title": "Buy paint"})
    await call(session, "create_task", {"project": "hallway", "title": "Paint"})
    await call(session, "set_task_done", {"task_id": a["id"], "done": True})
    [p] = (await call(session, "list_projects"))["result"]
    assert (p["percent"], p["open_count"], p["health"]) == (50, 1, "on_track")
    assert p["next_task"]["title"] == "Paint"


@with_session
async def test_set_project_goal_and_clear_it(session) -> None:
    await call(session, "create_project", {"name": "P"})
    p = await call(
        session, "set_project_goal", {"project": "p", "goal": "Done!", "target_date": "2030-05-01"}
    )
    assert p["goal"] == "Done!" and p["target_date"] == "2030-05-01" and p["health"] is None
    p = await call(session, "set_project_goal", {"project": "p", "goal": "Still"})
    assert p["target_date"] == "2030-05-01"  # omitted: kept
    p = await call(session, "set_project_goal", {"project": "p", "goal": "", "target_date": ""})
    assert p["goal"] is None and p["target_date"] is None and p["health"] is None
    assert "YYYY-MM-DD" in await call_error(
        session, "set_project_goal", {"project": "p", "goal": "g", "target_date": "soon"}
    )


@with_session
async def test_complete_project_refuses_open_tasks_and_close_does_not(session) -> None:
    await call(session, "create_project", {"name": "P", "goal": "g"})
    t = await call(session, "create_task", {"project": "p", "title": "open"})
    message = await call_error(session, "complete_project", {"project": "p"})
    assert "1 task(s) are still open" in message and "close the project" in message
    closed = await call(session, "set_project_status", {"project": "p", "status": "closed"})
    assert closed["status"] == "closed" and closed["health"] == "closed"
    assert (await call(session, "list_projects")) == {"result": []}
    [p] = (await call(session, "list_projects", {"include_finished": True}))["result"]
    assert p["status"] == "closed"

    back = await call(session, "set_project_status", {"project": "p", "status": "active"})
    assert back["status"] == "active"
    await call(session, "set_task_done", {"task_id": t["id"], "done": True})
    [p] = (await call(session, "list_projects"))["result"]
    assert p["health"] == "ready_to_complete"
    done = await call(session, "complete_project", {"project": "p"})
    assert done["status"] == "done" and done["percent"] == 100


@with_session
async def test_paused_project_is_out_of_next_steps(session) -> None:
    await call(session, "create_project", {"name": "Go"})
    await call(session, "create_project", {"name": "Wait"})
    await call(session, "create_project", {"name": "Empty"})
    await call(session, "create_task", {"project": "go", "title": "g1"})
    await call(session, "create_task", {"project": "wait", "title": "w1"})
    paused = await call(session, "set_project_status", {"project": "wait", "status": "paused"})
    assert paused["health"] == "paused"
    steps = (await call(session, "next_steps"))["result"]
    assert [(s["slug"], [t["title"] for t in s["tasks"]]) for s in steps] == [("go", ["g1"])]
    named = (await call(session, "next_steps", {"project": "wait"}))["result"]
    assert [s["slug"] for s in named] == ["wait"] and named[0]["status"] == "paused"


@with_session
async def test_next_steps_follow_roadmap_order_and_limit(session) -> None:
    await call(session, "create_project", {"name": "P"})
    await call(session, "create_task", {"project": "p", "title": "backlog item"})
    g = await call(session, "create_group", {"project": "p", "name": "Phase 1"})
    await call(session, "create_group", {"project": "p", "name": "Phase 2"})
    await call(session, "create_task", {"project": "p", "title": "p2 a", "group": "Phase 2"})
    first = await call(
        session, "create_task", {"project": "p", "title": "p1 a", "group": "Phase 1"}
    )
    await call(session, "create_task", {"project": "p", "title": "p1 b", "group": g["id"]})
    await call(session, "set_task_done", {"task_id": first["id"], "done": True})
    [steps] = (await call(session, "next_steps", {"limit": 2}))["result"]
    assert [t["title"] for t in steps["tasks"]] == ["p1 b", "p2 a"]
    assert steps["next_task"]["title"] == "p1 b"
    listing = await call(session, "list_tasks", {"project": "p"})
    assert listing["project"]["next_task"]["title"] == "p1 b"
    assert [g["name"] for g in listing["groups"]] == ["Phase 1", "Phase 2"]
    assert listing["groups"][0]["percent"] == 50
    assert [t["title"] for t in listing["backlog"]] == ["backlog item"]


@with_session
async def test_set_group_target_date_and_clear(session) -> None:
    await call(session, "create_project", {"name": "P"})
    g = await call(session, "create_group", {"project": "p", "name": "Phase 1"})
    dated = await call(
        session, "set_group_target_date", {"group_id": g["id"], "target_date": "2030-03-01"}
    )
    assert dated["target_date"] == "2030-03-01"
    [group] = (await call(session, "list_tasks", {"project": "p"}))["groups"]
    assert group["target_date"] == "2030-03-01"
    cleared = await call(session, "set_group_target_date", {"group_id": g["id"], "target_date": ""})
    assert cleared["target_date"] is None
    assert "YYYY-MM-DD" in await call_error(
        session, "set_group_target_date", {"group_id": g["id"], "target_date": "March"}
    )
    assert "not a valid id" in await call_error(
        session, "set_group_target_date", {"group_id": "junk", "target_date": ""}
    )


@with_session
async def test_task_size_due_date_and_assignee(session) -> None:
    await call(session, "create_project", {"name": "P"})
    t = await call(
        session,
        "create_task",
        {"project": "p", "title": "t", "size": "m", "due_date": "2030-02-02", "assignee": " Sam "},
    )
    assert (t["size"], t["due_date"], t["assignee"]) == ("M", "2030-02-02", "Sam")
    kept = await call(session, "update_task", {"task_id": t["id"], "title": "renamed"})
    assert (kept["size"], kept["due_date"], kept["assignee"]) == ("M", "2030-02-02", "Sam")
    cleared = await call(
        session, "update_task", {"task_id": t["id"], "size": "", "due_date": "", "assignee": ""}
    )
    assert (cleared["size"], cleared["due_date"], cleared["assignee"]) == (None, None, None)
    sized = await call(session, "update_task", {"task_id": t["id"], "size": "xl"})
    assert sized["size"] == "XL"
    assert "size must be one of XS, S, M, L, XL" in await call_error(
        session, "update_task", {"task_id": t["id"], "size": "huge"}
    )
    assert "size must be one of" in await call_error(
        session, "create_task", {"project": "p", "title": "x", "size": "tiny"}
    )
    assert "due_date must be a date" in await call_error(
        session, "create_task", {"project": "p", "title": "x", "due_date": "tomorrow"}
    )


# --- search (issue #40) --------------------------------------------------------


@with_session
async def test_search_tasks_finds_projects_and_tasks(session) -> None:
    await call(session, "create_project", {"name": "Hallway", "goal": "Hallway painted"})
    await call(session, "create_project", {"name": "Garden"})
    g = await call(session, "create_group", {"project": "hallway", "name": "Prep"})
    await call(session, "create_task", {"project": "hallway", "title": "Tape edges"})
    task = await call(
        session,
        "create_task",
        {
            "project": "hallway",
            "title": "Buy PAINT",
            "group": "Prep",
            "size": "s",
            "due_date": "2030-02-02",
            "assignee": "Sam",
        },
    )
    await call(session, "create_task", {"project": "garden", "title": "Dig", "notes": "paint pots"})

    found = await call(session, "search_tasks", {"query": "paint"})
    assert [p["slug"] for p in found["projects"]] == ["hallway"]
    assert found["projects"][0]["goal"] == "Hallway painted"
    assert [t["title"] for t in found["tasks"]] == ["Buy PAINT", "Dig"]
    assert found["tasks"][0] == {
        "id": task["id"],
        "title": "Buy PAINT",
        "done": False,
        "size": "S",
        "due_date": "2030-02-02",
        "assignee": "Sam",
        "project": {"name": "Hallway", "slug": "hallway"},
        "group": {"id": g["id"], "name": "Prep"},
    }
    assert found["tasks"][1]["group"] is None
    assert found["tasks"][1]["project"]["slug"] == "garden"

    one = await call(session, "search_tasks", {"query": "paint", "project": "garden"})
    assert [t["title"] for t in one["tasks"]] == ["Dig"]
    assert [p["slug"] for p in one["projects"]] == ["hallway"]


@with_session
async def test_search_tasks_done_archived_limit_and_no_match(session) -> None:
    await call(session, "create_project", {"name": "Paint", "goal": "g"})
    done = await call(session, "create_task", {"project": "paint", "title": "paint a wall"})
    await call(session, "create_task", {"project": "paint", "title": "paint a door"})
    await call(session, "set_task_done", {"task_id": done["id"], "done": True})

    open_only = await call(session, "search_tasks", {"query": "paint"})
    assert [t["title"] for t in open_only["tasks"]] == ["paint a door"]
    everything = await call(session, "search_tasks", {"query": "paint", "include_done": True})
    assert [t["title"] for t in everything["tasks"]] == ["paint a wall", "paint a door"]
    assert [t["done"] for t in everything["tasks"]] == [True, False]

    capped = await call(
        session, "search_tasks", {"query": "paint", "include_done": True, "limit": 1}
    )
    assert [t["title"] for t in capped["tasks"]] == ["paint a wall"]

    assert await call(session, "search_tasks", {"query": "zebra"}) == {"projects": [], "tasks": []}
    assert await call(session, "search_tasks", {"query": " "}) == {"projects": [], "tasks": []}

    await call(session, "archive_project", {"project": "paint"})
    assert await call(session, "search_tasks", {"query": "paint", "include_done": True}) == {
        "projects": [],
        "tasks": [],
    }
    assert "project not found" in await call_error(
        session, "search_tasks", {"query": "paint", "project": "nope"}
    )
