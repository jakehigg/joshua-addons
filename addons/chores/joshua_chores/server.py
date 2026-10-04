"""The chores addon server: MCP at ``/mcp``, ``/healthz``, and the built UI at ``/``.

The addon follows the contract in ``docs/adding-an-addon.md``. It serves MCP
at ``/mcp`` over streamable HTTP, the transport the joshua-ai gateway expects
from a ``type: http`` upstream. ``GET /healthz`` stays open. When
``ADDON_TOKEN`` is set, ``/mcp`` needs ``Authorization: Bearer <ADDON_TOKEN>``.
When ``ADDON_TOKEN`` is not set, the docker network is the boundary.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from joshua_chores.database import build_engine, build_sessionmaker
from joshua_chores.log import get_logger
from joshua_chores.migrations import run_migrations

logger = get_logger("chores")

# Only ``/mcp`` needs a bearer token. ``/healthz`` and the UI at ``/`` stay
# open.
PROTECTED_PREFIX = "/mcp"

# ``lifespan`` sets this at startup and clears it at shutdown. A tool that
# runs outside the lifespan gets an error, not a missing database.
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _session_factory() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("the database is not ready; the server lifespan has not started")
    return _sessionmaker


@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[dict[str, Any]]:
    """Build the engine, run the migrations, and open the session factory.

    This runs one time, when the streamable-HTTP session manager starts (see
    ``build_app``). It reads ``DATABASE_URL`` and ``CHORES_DB`` here, not at
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


mcp = MCPServer(name="chores", lifespan=lifespan)


@mcp.tool()
async def ping() -> dict[str, Any]:
    """Check that the chores addon is up. Returns ``{"ok": true}``."""
    return {"ok": True}


class BearerAuthMiddleware:
    """Require ``Authorization: Bearer <token>`` on ``PROTECTED_PREFIX`` only.

    ``/healthz`` and the UI at ``/`` stay open. When ``token`` is ``None``
    (no ``ADDON_TOKEN``), the middleware checks nothing: the docker network
    is the boundary.
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
    """Build the ASGI app of the addon: MCP, the built UI, and the auth wrapper.

    ``host="0.0.0.0"`` turns off the loopback-only DNS-rebinding guard of the
    SDK. A caller reaches this addon by its compose or cluster hostname (for
    example ``http://chores:8000/mcp``), never by ``localhost``, and the guard
    would refuse each real request.

    The built UI mounts on the ``Starlette`` instance that the MCP SDK returns,
    after ``/mcp`` and ``/healthz``. Starlette tries routes in the order they
    were added, so those two match first and the catch-all mount at ``/``
    matches last. The mount does not change the lifespan of that instance, so
    the database opens the same way with or without the UI.
    """
    from joshua_chores.static import build_static_app

    inner = mcp.streamable_http_app(streamable_http_path="/mcp", host="0.0.0.0")
    static_app = build_static_app()
    if static_app is not None:
        inner.mount("/", static_app)

    token = os.environ.get("ADDON_TOKEN") or None
    return BearerAuthMiddleware(inner, token)
