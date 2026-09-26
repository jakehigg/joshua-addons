"""The negative set: no bearer, a wrong bearer, a read-only caller, and /healthz."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import httpx
import pytest
from conftest import READER, WRITER, mcp_session, snapshot
from joshua_mcp.config import Caller, Settings
from joshua_mcp.server import build_app, find_caller


async def post_mcp(app, headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post("/mcp", headers=headers, json={})


async def test_a_missing_bearer_gets_401(app) -> None:
    response = await post_mcp(app, {})
    assert response.status_code == 401


async def test_a_wrong_bearer_gets_401(app) -> None:
    response = await post_mcp(app, {"Authorization": "Bearer nope"})
    assert response.status_code == 401


async def test_a_bearer_without_the_scheme_gets_401(app) -> None:
    response = await post_mcp(app, {"Authorization": WRITER})
    assert response.status_code == 401


async def test_healthz_is_open_and_names_nothing(app, data_dir) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    for secret in (str(data_dir), "laptop", "wiki", WRITER, READER):
        assert secret not in response.text


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("write_page", {"path": "notes/x.md", "markdown": "x"}),
        ("write_journal_entry", {"slug": "x", "markdown": "x"}),
    ],
)
async def test_a_read_only_caller_gets_403_on_every_write_tool(app, wiki, tool, args) -> None:
    before = snapshot(wiki)
    async with mcp_session(app, token=READER) as session:
        result = await session.call_tool(tool, args)
    assert result.is_error is True
    assert result.content[0].text.endswith("403 forbidden: the caller 'laptop-ci' is read-only")
    assert snapshot(wiki) == before


async def test_a_read_only_caller_can_read(app) -> None:
    async with mcp_session(app, token=READER) as session:
        result = await session.call_tool("read_page", {"path": "garden.md"})
    assert result.is_error is not True


async def test_every_write_tool_is_covered_by_the_negative_test() -> None:
    from joshua_mcp.server import mcp

    tools = await mcp.list_tools()
    writes = {tool.name for tool in tools if tool.name.startswith("write_")}
    assert writes == {"write_page", "write_journal_entry"}


async def test_with_no_bearer_configured_the_network_is_the_boundary(data_dir) -> None:
    settings = Settings(data_dir=data_dir, timezone=ZoneInfo("UTC"))
    app = build_app(settings)
    async with mcp_session(app, token=None) as session:
        result = await session.call_tool(
            "write_page", {"path": "open.md", "markdown": "open network"}
        )
    assert result.is_error is not True
    assert "author: anonymous" in (data_dir / "wiki" / "open.md").read_text()


async def test_an_anonymous_caller_can_be_read_only(data_dir) -> None:
    settings = Settings(data_dir=data_dir, timezone=ZoneInfo("UTC"), anonymous_readonly=True)
    app = build_app(settings)
    async with mcp_session(app, token=None) as session:
        result = await session.call_tool("write_page", {"path": "open.md", "markdown": "x"})
    assert "403 forbidden" in result.content[0].text
    assert not (data_dir / "wiki" / "open.md").exists()


def test_find_caller(settings) -> None:
    assert find_caller(settings, f"Bearer {WRITER}") == Caller(name="laptop")
    assert find_caller(settings, f"Bearer {READER}") == Caller(name="laptop-ci", readonly=True)
    assert find_caller(settings, "Bearer ") is None
    assert find_caller(settings, None) is None
    assert find_caller(settings, f"Basic {WRITER}") is None
