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
async def test_tool_names_are_exactly_the_fourteen(session) -> None:
    names = [t.name for t in (await session.list_tools()).tools]
    assert len(names) == 14
    assert set(names) == TOOL_NAMES


@with_session
async def test_tools_declare_the_old_arguments(session) -> None:
    tools = {t.name: t.input_schema for t in (await session.list_tools()).tools}
    props = {
        n: (set(s.get("properties", {})), set(s.get("required", []))) for n, s in tools.items()
    }
    assert props == {
        "list_projects": (set(), set()),
        "create_project": ({"name"}, {"name"}),
        "rename_project": ({"project", "name"}, {"project", "name"}),
        "archive_project": ({"project"}, {"project"}),
        "list_tasks": ({"project", "include_done"}, {"project"}),
        "create_task": ({"project", "title", "notes", "group"}, {"project", "title"}),
        "update_task": ({"task_id", "title", "notes"}, {"task_id"}),
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
                "task_count": 0,
                "done_count": 0,
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
    assert [t["title"] for t in listing["ungrouped"]] == ["Buy paint", "Tape"]
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
    assert [t["title"] for t in listing["ungrouped"]] == ["loose"]


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
    assert [t["title"] for t in listing["ungrouped"]] == ["b", "a"]
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
    assert listing["ungrouped"] == []
    assert [t["title"] for t in listing["groups"][0]["tasks"]] == ["a"]


@with_session
async def test_move_task_to_another_project(session) -> None:
    await call(session, "create_project", {"name": "Src"})
    await call(session, "create_project", {"name": "Dst"})
    t = await call(session, "create_task", {"project": "src", "title": "roaming"})
    moved = await call(session, "move_task", {"task_id": t["id"], "to_project": "dst"})
    assert moved["id"] == t["id"]
    assert (await call(session, "list_tasks", {"project": "src"}))["ungrouped"] == []
    assert [
        x["title"] for x in (await call(session, "list_tasks", {"project": "dst"}))["ungrouped"]
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
    assert renamed == {"id": g["id"], "name": "New", "position": 1024.0}


@with_session
async def test_delete_group_ungroups_tasks(session) -> None:
    await call(session, "create_project", {"name": "P"})
    g = await call(session, "create_group", {"project": "p", "name": "G"})
    await call(session, "create_task", {"project": "p", "title": "a", "group": g["id"]})
    assert await call(session, "delete_group", {"group_id": g["id"]}) == {"ok": True}
    listing = await call(session, "list_tasks", {"project": "p"})
    assert listing["groups"] == []
    assert [t["title"] for t in listing["ungrouped"]] == ["a"]


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
