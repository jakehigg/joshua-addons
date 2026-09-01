"""The hello addon: one MCP tool, served over streamable HTTP.

The addon is the pattern every addon in this repository copies. It serves MCP
at ``/mcp``, the same streamable-HTTP transport and path the joshua-ai gateway
expects from a ``type: http`` upstream. ``GET /healthz`` stays open. Every
other route needs ``Authorization: Bearer <ADDON_TOKEN>`` when ``ADDON_TOKEN``
is set; with no ``ADDON_TOKEN``, the docker network is the boundary.
"""

from __future__ import annotations

import hmac
import os

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from joshua_hello.log import get_logger

logger = get_logger("hello")

OPEN_PATHS = frozenset({"/healthz"})

mcp = MCPServer(name="hello")


@mcp.tool()
def hello(name: str) -> str:
    """Greet ``name``."""
    return f"Hello, {name}!"


class BearerAuthMiddleware:
    """Require ``Authorization: Bearer <token>`` on every path except ``OPEN_PATHS``.

    Skips the check entirely when ``token`` is ``None`` (no ``ADDON_TOKEN``
    configured): the docker network is the boundary in that case.
    """

    def __init__(self, app: ASGIApp, token: str | None) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.token is None or scope["path"] in OPEN_PATHS:
            await self.app(scope, receive, send)
            return

        headers = dict(scope["headers"])
        raw = headers.get(b"authorization", b"").decode("latin-1")
        prefix = "Bearer "
        provided = raw[len(prefix) :] if raw.startswith(prefix) else ""
        if not provided or not hmac.compare_digest(provided, self.token):
            logger.warning({"message": "rejected request: missing or wrong token"})
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


async def healthz(request: Request) -> Response:
    return JSONResponse({"ok": True})


mcp.custom_route("/healthz", methods=["GET"])(healthz)


def build_app() -> ASGIApp:
    """Build the addon's ASGI app: the MCP routes plus the auth wrapper.

    ``host="0.0.0.0"`` turns off the SDK's loopback-only DNS-rebinding guard: a
    caller reaches this addon by its compose or cluster hostname (for example
    ``http://hello:8000/mcp``), never by ``localhost``, and the guard would
    otherwise reject every real request.
    """
    inner = mcp.streamable_http_app(streamable_http_path="/mcp", host="0.0.0.0")
    token = os.environ.get("ADDON_TOKEN") or None
    return BearerAuthMiddleware(inner, token)
