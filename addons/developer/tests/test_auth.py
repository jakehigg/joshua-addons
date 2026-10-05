"""The negative set: no bearer, a wrong bearer, a caller with no person, and /healthz."""

from __future__ import annotations

import httpx
import pytest
from conftest import (
    ADDON,
    ALEX,
    MIA,
    error,
    make_settings,
    mcp_session,
    mcp_sessions,
    payload,
)
from joshua_developer.config import Caller
from joshua_developer.server import build_app, find_caller


async def post_mcp(app, headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post("/mcp", headers=headers, json={})


async def test_a_missing_bearer_gets_401(app) -> None:
    assert (await post_mcp(app, {})).status_code == 401


async def test_a_wrong_bearer_gets_401(app) -> None:
    assert (await post_mcp(app, {"Authorization": "Bearer nope"})).status_code == 401


async def test_a_bearer_without_the_scheme_gets_401(app) -> None:
    assert (await post_mcp(app, {"Authorization": ALEX})).status_code == 401


async def test_an_unknown_path_needs_a_bearer_too(app) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/anything")
    assert response.status_code == 401


async def test_healthz_is_open_and_names_nothing(app, data_dir) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    for secret in (str(data_dir), "alex", "mia", ALEX, MIA, ADDON):
        assert secret not in response.text


def test_find_caller(settings) -> None:
    assert find_caller(settings, f"Bearer {ALEX}") == Caller(person="alex")
    assert find_caller(settings, f"Bearer {MIA}") == Caller(person="mia")
    assert find_caller(settings, f"Bearer {ADDON}") == Caller(person=None)
    assert find_caller(settings, "Bearer ") is None
    assert find_caller(settings, None) is None
    assert find_caller(settings, f"Basic {ALEX}") is None


async def test_the_person_comes_from_the_bearer(app) -> None:
    async with mcp_session(app, token=MIA) as session:
        result = payload(await session.call_tool("get_settings", {}))
    assert result["person"] == "mia"


PERSON_TOOLS = [
    ("develop", {"repo": "github.com/example-home/app", "brief": "x"}),
    ("rework", {"repo": "github.com/example-home/app", "pr": 1, "feedback": "x"}),
    ("answer", {"task_id": "x", "text": "x"}),
    ("get_settings", {}),
    ("set_settings", {"notify": "x"}),
]


@pytest.mark.parametrize(("tool", "args"), PERSON_TOOLS)
async def test_the_addon_token_cannot_use_a_person_tool(app, tool: str, args: dict) -> None:
    async with mcp_session(app, token=ADDON) as session:
        result = await session.call_tool(tool, args)
        tasks = payload(await session.call_tool("list_tasks", {}))
    assert error(result).endswith("403 forbidden: this tool needs a person")
    assert tasks["tasks"] == []


async def test_every_person_tool_is_in_the_negative_test() -> None:
    from joshua_developer.server import mcp

    tools = {tool.name for tool in await mcp.list_tools()}
    read_tools = {"task_status", "task_output", "list_tasks", "list_personas"}
    assert tools - read_tools == {name for name, _ in PERSON_TOOLS}


async def test_the_addon_token_may_read(app) -> None:
    async with mcp_sessions(app, ALEX, ADDON) as (alex, session):
        task_id = payload(
            await alex.call_tool("develop", {"repo": "github.com/example-home/app", "brief": "x"})
        )["task_id"]
        status = payload(await session.call_tool("task_status", {"task_id": task_id}))
        output = payload(await session.call_tool("task_output", {"task_id": task_id}))
        tasks = payload(await session.call_tool("list_tasks", {}))
        personas = payload(await session.call_tool("list_personas", {}))
    assert status["person"] == "alex"
    assert output["status"] == "success"
    assert [t["task_id"] for t in tasks["tasks"]] == [task_id]
    assert personas["default"] == "opus"


async def test_with_no_bearer_configured_the_network_is_the_boundary(data_dir) -> None:
    app = build_app(make_settings(data_dir, tokens={}), stub_delay_s=0)
    async with mcp_session(app, token=None) as session:
        personas = await session.call_tool("list_personas", {})
        develop = await session.call_tool(
            "develop", {"repo": "github.com/example-home/app", "brief": "x"}
        )
    assert personas.is_error is not True
    assert error(develop).endswith("403 forbidden: this tool needs a person")
