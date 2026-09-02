"""End-to-end over the real MCP transport: tool listing, a call, and the
database the server's own lifespan opens on startup (no ``ADDON_TOKEN``).
"""

from __future__ import annotations

from conftest import mcp_session


async def test_lists_every_tool(app_factory) -> None:
    app = app_factory(None)
    async with mcp_session(app) as session:
        names = {t.name for t in (await session.list_tools()).tools}
    assert names == {
        "record_purchase",
        "get_inventory",
        "get_item_history",
        "get_item_cost",
        "consume_items",
        "set_preferred_store",
        "set_purchase_cost",
        "set_purchase_store",
        "delete_purchase",
        "delete_item",
        "add_alias",
        "list_aliases",
        "resolve_product",
        "set_preferred_product",
        "get_price_stats",
        "import_data",
    }


async def test_record_purchase_over_mcp(app_factory) -> None:
    app = app_factory(None)
    async with mcp_session(app) as session:
        result = await session.call_tool(
            "record_purchase",
            {
                "items": [{"name": "Eggs", "cost": 2.5}],
                "purchased_at": "2026-08-01",
            },
        )
        assert result.is_error is not True
        assert result.structured_content["items"][0]["resolved_to"] == "Eggs"

        inventory = await session.call_tool("get_inventory", {})
        assert inventory.is_error is not True


async def test_unknown_item_error_names_the_item_with_no_traceback(app_factory) -> None:
    app = app_factory(None)
    async with mcp_session(app) as session:
        result = await session.call_tool("get_item_history", {"item_name": "Nonexistent"})
        assert result.is_error is True
        [block] = result.content
        assert "Nonexistent" in block.text
        assert "Traceback" not in block.text
