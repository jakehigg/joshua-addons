"""Where a tool call came from: the workspace stamp, and the links in a result.

On a call from a workspace conversation, the gateway sets
``params._meta["joshua/workspace"]`` to the workspace id. A call from any other
chat has no such key. ``origin_of`` reads it from the request context of the
tool. ``with_links`` adds MCP ``resource_link`` blocks to a result, for a call
that has the stamp.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import Context
from mcp_types import CallToolResult, ContentBlock, ResourceLink, TextContent

WORKSPACE_META_KEY = "joshua/workspace"


def origin_of(ctx: Context) -> str | None:
    """The workspace id in the ``_meta`` of this call, or None.

    A missing context, a missing ``_meta``, and a value that is not a
    non-empty string all give None.
    """
    try:
        meta = ctx.request_context.meta
    except ValueError:
        return None
    value = meta.get(WORKSPACE_META_KEY) if meta else None
    return value if isinstance(value, str) and value else None


def task_links(task: dict[str, Any] | None) -> list[ResourceLink]:
    """The links for a task that a person may open: its pull request, when it has one."""
    url = (task or {}).get("pr_url")
    if not url:
        return []
    number = task.get("pr_number") if task else None
    return [
        ResourceLink(
            uri=str(url),
            name=f"Pull request #{number}" if number else "Pull request",
            description=f"The pull request of developer task {task.get('task_id') if task else ''}",
            mime_type="text/html",
        )
    ]


def with_links(
    result: dict[str, Any], links: list[ResourceLink]
) -> dict[str, Any] | CallToolResult:
    """``result`` as it is, or with ``links`` after its text, when there are links."""
    if not links:
        return result
    content: list[ContentBlock] = [TextContent(text=json.dumps(result, indent=2, default=str))]
    content.extend(links)
    return CallToolResult(
        content=content, structured_content=json.loads(json.dumps(result, default=str))
    )
