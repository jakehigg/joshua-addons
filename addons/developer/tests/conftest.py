"""Shared fixtures for the developer addon tests.

Every test works on a database in a temporary directory and drives the ASGI
app in-process: no network, no Docker, no worker.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from joshua_developer.config import Caller, Settings, parse_config
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.types import ASGIApp

ALEX = "alex-token"
MIA = "mia-token"
ADDON = "addon-token"

CONFIG: dict[str, Any] = {
    "network": "off",
    "default_persona": "opus",
    "max_workers": 2,
    "platforms": {
        "github.com": {"kind": "github", "token_env": "GITHUB_TOKEN"},
        "gitlab.example.net": {"kind": "gitlab", "token_env": "GITLAB_TOKEN"},
    },
    "repos": ["github.com/example-home/*"],
    "people": {
        "alex": {
            "git": {"name": "Alex Example", "email": "alex@users.noreply.github.com"},
            "default_persona": "sonnet",
            "notify": "telegram:dm:alex",
            "platforms": {"github.com": {"token_env": "GITHUB_TOKEN_ALEX"}},
            "repos": ["github.com/alex-example/*"],
        },
        "mia": {},
    },
}


def make_settings(data_dir: Path, **overrides: Any) -> Settings:
    config = json.loads(json.dumps(CONFIG))
    config.update(overrides.pop("config", {}))
    fields: dict[str, Any] = {
        "config": parse_config(config),
        "data_dir": data_dir,
        "tokens": {
            ALEX: Caller(person="alex"),
            MIA: Caller(person="mia"),
            ADDON: Caller(person=None),
        },
    }
    fields.update(overrides)
    return Settings(**fields)


@pytest.fixture(autouse=True)
def close_the_manager():
    """Close the store of the app a test built, so no database stays open."""
    yield
    from joshua_developer import server

    if server._manager is not None:
        server._manager.store.close()
        server._manager = None


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def settings(data_dir: Path) -> Settings:
    return make_settings(data_dir)


@pytest.fixture
def app(settings: Settings) -> ASGIApp:
    """The app with a stub runtime that records its report at once."""
    from joshua_developer.server import build_app

    return build_app(settings, stub_delay_s=0)


@pytest.fixture
def holding_app(settings: Settings) -> ASGIApp:
    """The app with a stub runtime that never records a report: tasks stay running."""
    from joshua_developer.server import build_app

    return build_app(settings, stub_delay_s=None)


def payload(result) -> dict:
    assert result.is_error is not True, result.content
    [block] = result.content
    return json.loads(block.text)


def error(result) -> str:
    assert result.is_error is True
    return result.content[0].text


@contextlib.asynccontextmanager
async def _client_session(app: ASGIApp, token: str | None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    transport = httpx2.ASGITransport(app=app)
    client = httpx2.AsyncClient(
        transport=transport, base_url="http://testserver", headers=headers, timeout=10
    )
    try:
        async with streamable_http_client("http://testserver/mcp", http_client=client) as (
            read,
            write_stream,
        ):
            async with ClientSession(read, write_stream) as session:
                await session.initialize()
                yield session
    finally:
        await client.aclose()


@contextlib.asynccontextmanager
async def mcp_sessions(app: ASGIApp, *tokens: str | None):
    """Open one MCP session for each token to ``app``, over an in-process ASGI transport.

    An ASGI transport never sends the ``lifespan`` protocol, so enter the
    session manager by hand, as the other addon tests do. The session
    manager runs once for each app, so every session of a test opens here.
    """
    from joshua_developer.server import mcp as addon_mcp

    async with addon_mcp.session_manager.run():
        async with contextlib.AsyncExitStack() as stack:
            sessions = [await stack.enter_async_context(_client_session(app, t)) for t in tokens]
            yield sessions


@contextlib.asynccontextmanager
async def mcp_session(app: ASGIApp, token: str | None = ALEX):
    """Open one MCP session to ``app`` with ``token``."""
    async with mcp_sessions(app, token) as (session,):
        yield session
