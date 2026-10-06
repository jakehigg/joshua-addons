"""The server reports ``ADDON_VERSION`` as ``serverInfo.version`` at ``initialize``."""

from __future__ import annotations

import importlib

import httpx2
import pytest
from joshua_hello import server
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


@pytest.fixture
def reload_server(monkeypatch):
    """Reload the server module with ``ADDON_VERSION`` set as given; restore after."""

    def _reload(version: str | None):
        if version is None:
            monkeypatch.delenv("ADDON_VERSION", raising=False)
        else:
            monkeypatch.setenv("ADDON_VERSION", version)
        monkeypatch.delenv("ADDON_TOKEN", raising=False)
        return importlib.reload(server)

    yield _reload
    monkeypatch.delenv("ADDON_VERSION", raising=False)
    importlib.reload(server)


async def _server_info(module):
    app = module.build_app()
    transport = httpx2.ASGITransport(app=app)
    client = httpx2.AsyncClient(transport=transport, base_url="http://testserver", timeout=10)
    try:
        async with module.mcp.session_manager.run():
            async with streamable_http_client("http://testserver/mcp", http_client=client) as (
                read,
                write,
            ):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    return init.server_info
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_initialize_reports_the_build_version(reload_server) -> None:
    info = await _server_info(reload_server("2026.10.1"))
    assert info.name == "hello"
    assert info.version == "2026.10.1"


@pytest.mark.asyncio
async def test_a_build_with_no_version_reports_dev(reload_server) -> None:
    info = await _server_info(reload_server(None))
    assert info.version == "dev"
