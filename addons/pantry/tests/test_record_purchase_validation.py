"""Validation rejections on ``record_purchase``: clean tool errors, no tracebacks."""

from __future__ import annotations

import pytest
from joshua_pantry import server
from mcp.server.mcpserver.exceptions import ToolError


async def test_empty_items_rejected(bound_db) -> None:
    with pytest.raises(ToolError, match="items is empty"):
        await server.record_purchase(items=[], purchased_at="2026-08-01")


async def test_missing_name_rejected(bound_db) -> None:
    with pytest.raises(ToolError, match="non-empty name"):
        await server.record_purchase(
            items=[server.PurchaseLine(name="   ")], purchased_at="2026-08-01"
        )


async def test_missing_date_rejected(bound_db) -> None:
    with pytest.raises(ToolError, match="purchased_at is required"):
        await server.record_purchase(items=[server.PurchaseLine(name="Eggs")], purchased_at="")


async def test_invalid_date_rejected(bound_db) -> None:
    with pytest.raises(ToolError, match="Could not parse purchased_at 'not-a-date'"):
        await server.record_purchase(
            items=[server.PurchaseLine(name="Eggs")], purchased_at="not-a-date"
        )
