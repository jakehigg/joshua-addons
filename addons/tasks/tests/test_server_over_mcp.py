"""The placeholder tool is callable over an in-process MCP session."""

from __future__ import annotations

from conftest import mcp_session


async def test_ping_is_listed_and_callable(app_factory) -> None:
    app = app_factory(None)
    async with mcp_session(app) as session:
        tools = await session.list_tools()
        assert [t.name for t in tools.tools] == ["ping"]
        result = await session.call_tool("ping", {})
    assert result.is_error is not True
    assert result.structured_content == {"result": "pong"}


async def test_session_factory_is_unset_outside_the_lifespan() -> None:
    import pytest
    from joshua_tasks import server

    with pytest.raises(RuntimeError, match="not ready"):
        server._session_factory()
