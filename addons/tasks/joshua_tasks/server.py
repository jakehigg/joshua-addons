"""The tasks addon: MCP tools for projects, groups, and tasks.

The addon is a copy of the pantry addon's shape (``docs/adding-an-addon.md``):
it serves MCP at ``/mcp``, the streamable-HTTP transport the joshua-ai gateway
expects from a ``type: http`` upstream. ``GET /healthz`` stays open. Every
other MCP route needs ``Authorization: Bearer <ADDON_TOKEN>`` when
``ADDON_TOKEN`` is set; with no ``ADDON_TOKEN``, the docker network is the
boundary.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from joshua_tasks.database import build_engine, build_sessionmaker
from joshua_tasks.log import get_logger
from joshua_tasks.migrations import run_migrations

logger = get_logger("tasks")

# Only ``/mcp`` needs a bearer token. ``/healthz``, ``/api/*`` (the REST API
# behind the web UI), and the UI itself at ``/`` stay open.
PROTECTED_PREFIX = "/mcp"

# Set by ``lifespan`` on startup, cleared on shutdown. A tool called before
# startup (or after shutdown) fails loudly instead of touching no database.
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _session_factory() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("the database is not ready; the server lifespan has not started")
    return _sessionmaker


@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[dict[str, Any]]:
    """Build the engine, run migrations, and open the session factory.

    Runs once, when the streamable-HTTP session manager starts (see
    ``build_app``). ``DATABASE_URL``/``TASKS_DB`` are read here, not at
    import time, so a caller (a test, for example) can set them first.
    """
    global _sessionmaker
    engine = build_engine()
    await run_migrations(engine)
    _sessionmaker = build_sessionmaker(engine)
    try:
        yield {}
    finally:
        _sessionmaker = None
        await engine.dispose()


mcp = MCPServer(name="tasks", lifespan=lifespan)


@mcp.tool()
async def ping() -> str:
    """Placeholder tool. Answers "pong". Removed when the real tools land (P5.3)."""
    return "pong"


class BearerAuthMiddleware:
    """Require ``Authorization: Bearer <token>`` on ``PROTECTED_PREFIX`` only.

    ``/healthz``, ``/api/*``, and the served UI at ``/`` stay open. Skips the
    check entirely when ``token`` is ``None`` (no ``ADDON_TOKEN``
    configured): the docker network is the boundary in that case.
    """

    def __init__(self, app: ASGIApp, token: str | None) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        protected = path == PROTECTED_PREFIX or path.startswith(PROTECTED_PREFIX + "/")
        if scope["type"] != "http" or self.token is None or not protected:
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
    """Build the addon's ASGI app: MCP, the REST API mount, the built UI, and the auth wrapper.

    ``host="0.0.0.0"`` turns off the SDK's loopback-only DNS-rebinding guard: a
    caller reaches this addon by its compose or cluster hostname (for example
    ``http://tasks:8000/mcp``), never by ``localhost``, and the guard would
    otherwise reject every real request.

    ``/api`` and the built frontend are mounted onto the same ``Starlette``
    instance the MCP SDK returns, after it registered ``/mcp`` and
    ``/healthz``. Starlette tries routes in registration order, so those two
    match first, then ``/api``, then the catch-all UI mount at ``/`` last.
    """
    from joshua_tasks.static import build_static_app

    inner = mcp.streamable_http_app(streamable_http_path="/mcp", host="0.0.0.0")
    # The REST API (P5.4) goes here. Empty until then: every /api path is a 404.
    inner.mount("/api", Starlette())
    static_app = build_static_app()
    if static_app is not None:
        inner.mount("/", static_app)

    token = os.environ.get("ADDON_TOKEN") or None
    return BearerAuthMiddleware(inner, token)
