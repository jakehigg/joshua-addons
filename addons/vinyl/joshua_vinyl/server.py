"""The vinyl addon: the browse interface, the bundle, and MCP, on one port.

The shape is the hello addon's. MCP is at ``/mcp``, the streamable-HTTP
transport the joshua-ai gateway expects from a ``type: http`` upstream.
``GET /healthz`` stays open. ``/mcp`` needs ``Authorization: Bearer
<ADDON_TOKEN>`` when ``ADDON_TOKEN`` is set. The interface at ``/``, the
bundle at ``/bundle``, the art at ``/art``, and ``/api/status`` are open, the
same posture as the pantry UI: the docker network or the ingress is the
boundary, not this addon.
"""

from __future__ import annotations

import asyncio
import hmac
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from joshua_vinyl import db
from joshua_vinyl.bundle import read_index
from joshua_vinyl.config import Settings, settings_from_env
from joshua_vinyl.log import get_logger
from joshua_vinyl.scheduler import run_daily
from joshua_vinyl.sync import META_LAST_RESULT, META_LAST_SYNC

logger = get_logger("vinyl")

PROTECTED_PREFIX = "/mcp"

# Set by ``build_app``. The lifespan and the status tool read it.
_settings: Settings | None = None


def _current_settings() -> Settings:
    if _settings is None:
        raise RuntimeError("build_app has not run")
    return _settings


def status(settings: Settings) -> dict[str, Any]:
    """The sync state: how many records, when the last sync ran, and how it ended."""
    index = read_index(settings.bundle_dir)
    result: dict[str, Any] = {
        "records": index["count"] if index else 0,
        "bundle_generated_at": index["generated_at"] if index else None,
        "last_sync": None,
        "last_result": None,
    }
    if settings.db_path.is_file():
        conn = db.connect(settings.db_path)
        try:
            result["last_sync"] = db.get_meta(conn, META_LAST_SYNC)
            result["last_result"] = db.get_meta(conn, META_LAST_RESULT)
        finally:
            conn.close()
    return result


def _scheduled_sync() -> None:
    from joshua_vinyl.cli import sync_once

    sync_once(_current_settings())


@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[dict[str, Any]]:
    """Start the nightly sync loop when a schedule and a token are both set."""
    settings = _current_settings()
    task: asyncio.Task[int] | None = None
    if settings.sync_time and settings.discogs_token:
        task = asyncio.create_task(run_daily(settings.sync_time, _scheduled_sync))
    else:
        logger.info({"message": "nightly sync off: no VINYL_SYNC_TIME or no DISCOGS_TOKEN"})
    try:
        yield {}
    finally:
        if task is not None:
            task.cancel()


mcp = MCPServer(name="vinyl", lifespan=lifespan)


@mcp.tool()
def vinyl_status() -> dict[str, Any]:
    """Report the sync state: record count, last sync time, and last result."""
    return status(_current_settings())


class BearerAuthMiddleware:
    """Require ``Authorization: Bearer <token>`` on ``PROTECTED_PREFIX`` only.

    Skips the check entirely when ``token`` is ``None`` (no ``ADDON_TOKEN``
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


async def api_status(request: Request) -> Response:
    return JSONResponse(status(_current_settings()))


mcp.custom_route("/healthz", methods=["GET"])(healthz)
mcp.custom_route("/api/status", methods=["GET"])(api_status)


def build_app() -> ASGIApp:
    """Build the ASGI app: MCP, the status route, the bundle, the art, and the UI.

    ``host="0.0.0.0"`` turns off the SDK's loopback-only DNS-rebinding guard:
    a caller reaches this addon by its compose or cluster hostname, never by
    ``localhost``, and the guard would otherwise reject every real request.

    Starlette tries routes in registration order, so ``/mcp``, ``/healthz``,
    and ``/api/status`` match first, then the two data mounts, then the UI at
    ``/`` last. The bundle and the art directories are created here, empty,
    because they do not exist before the first sync and a static mount needs
    a directory that exists.
    """
    global _settings
    _settings = settings_from_env()
    _settings.bundle_dir.mkdir(parents=True, exist_ok=True)
    _settings.art_dir.mkdir(parents=True, exist_ok=True)
    inner = mcp.streamable_http_app(streamable_http_path="/mcp", host="0.0.0.0")
    inner.mount("/bundle", StaticFiles(directory=str(_settings.bundle_dir)))
    inner.mount("/art", StaticFiles(directory=str(_settings.art_dir)))
    if _settings.static_dir.is_dir():
        inner.mount("/", StaticFiles(directory=str(_settings.static_dir), html=True))
    token = os.environ.get("ADDON_TOKEN") or None
    return BearerAuthMiddleware(inner, token)
