"""The hello tool as a plain function, and end to end over MCP."""

from __future__ import annotations

import pytest
from conftest import mcp_session
from joshua_hello.server import hello


def test_hello_greets_by_name() -> None:
    assert hello("Alex") == "Hello, Alex!"


def test_hello_uses_the_given_name_not_a_fixed_one() -> None:
    assert "Mia" in hello("Mia")
    assert "Alex" not in hello("Mia")


@pytest.mark.asyncio
async def test_hello_tool_over_mcp_with_no_token(app_factory) -> None:
    app = app_factory(None)
    async with mcp_session(app) as session:
        tools = (await session.list_tools()).tools
        assert [t.name for t in tools] == ["hello"]

        result = await session.call_tool("hello", {"name": "Alex"})
        assert result.is_error is not True
        [block] = result.content
        assert block.text == "Hello, Alex!"
