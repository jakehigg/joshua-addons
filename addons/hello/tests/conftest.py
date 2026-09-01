"""Shared fixtures for the hello addon tests.

Every test drives the ASGI app in-process (no network, no Docker). The MCP
tests use ``mcp.client.streamable_http`` over an ``httpx2.ASGITransport``, the
same client the joshua-ai gateway uses to reach a ``type: http`` upstream.
"""

from __future__ import annotations

import contextlib

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.types import ASGIApp


@pytest.fixture
def app_factory(monkeypatch):
    """Return a function that builds a fresh app with ``ADDON_TOKEN`` set as given."""

    def _build(token: str | None) -> ASGIApp:
        if token is None:
            monkeypatch.delenv("ADDON_TOKEN", raising=False)
        else:
            monkeypatch.setenv("ADDON_TOKEN", token)
        from joshua_hello.server import build_app

        return build_app()

    return _build


@contextlib.asynccontextmanager
async def mcp_session(app: ASGIApp, headers: dict[str, str] | None = None):
    """Open an MCP session to ``app`` at ``/mcp`` over an in-process ASGI transport.

    An ASGI transport never sends the ``lifespan`` protocol, so the session
    manager that ``build_app`` wires up through Starlette's lifespan never
    starts on its own. Enter it by hand, the same way ``joshua_gateway.upstream``
    enters an upstream's session manager.
    """
    from joshua_hello.server import mcp as hello_mcp

    transport = httpx2.ASGITransport(app=app)
    client = httpx2.AsyncClient(
        transport=transport, base_url="http://testserver", headers=headers or {}, timeout=10
    )
    try:
        async with hello_mcp.session_manager.run():
            async with streamable_http_client("http://testserver/mcp", http_client=client) as (
                read,
                write,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
    finally:
        await client.aclose()
