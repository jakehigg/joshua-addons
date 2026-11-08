"""Shared fixtures for the tasks addon tests."""

from __future__ import annotations

import contextlib

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.types import ASGIApp


@pytest.fixture
def app_factory(monkeypatch, tmp_path):
    """Return a function that builds a fresh app, with ``ADDON_TOKEN`` and the
    database set as given.
    """

    def _build(token: str | None, db_path=None):
        if token is None:
            monkeypatch.delenv("ADDON_TOKEN", raising=False)
        else:
            monkeypatch.setenv("ADDON_TOKEN", token)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("TASKS_DB", str(db_path or (tmp_path / "tasks.db")))

        from joshua_tasks.server import build_app

        return build_app()

    return _build


@contextlib.asynccontextmanager
async def mcp_session(app: ASGIApp, headers: dict[str, str] | None = None):
    """Open an MCP session to ``app`` at ``/mcp`` over an in-process ASGI transport.

    An ASGI transport never sends the ``lifespan`` protocol, so the session
    manager never starts on its own. Enter it by hand; this also runs the
    server's own lifespan (engine build + migrations).
    """
    from joshua_tasks.server import mcp as tasks_mcp

    transport = httpx2.ASGITransport(app=app)
    client = httpx2.AsyncClient(
        transport=transport, base_url="http://testserver", headers=headers or {}, timeout=10
    )
    try:
        async with tasks_mcp.session_manager.run():
            async with streamable_http_client("http://testserver/mcp", http_client=client) as (
                read,
                write,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
    finally:
        await client.aclose()
