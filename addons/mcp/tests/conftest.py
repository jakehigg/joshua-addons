"""Shared fixtures for the joshua-mcp addon tests.

Every test works on a wiki in a temporary directory and drives the ASGI app
in-process: no network, no Docker, no cluster volume.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx2
import pytest
from joshua_mcp.config import Caller, Settings
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.types import ASGIApp

WRITER = "laptop-token"
READER = "ci-token"
FIXED_NOW = datetime(2026, 9, 26, 15, 0, tzinfo=UTC)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A data volume with a small wiki: pages, profiles, docs, and a journal."""
    data = tmp_path / "data"
    wiki = data / "wiki"
    write(wiki / "Home.md", "# Home\n\nThe front page.\n")
    write(
        wiki / "recipes" / "bread.md",
        "---\ntitle: Sourdough bread\n---\n\nFeed the starter the night before. Bake at 250 C.\n",
    )
    write(wiki / "garden.md", "# Garden\n\nThe tomatoes need water every day in August.\n")
    write(wiki / "people" / "jake.md", "# Jake\n\nThe profile.\n")
    write(wiki / "joshua-docs" / "index.md", "# Docs\n")
    write(wiki / "attachments" / "2026" / "09" / "note.md", "binary folder, never listed\n")
    write(wiki / ".trash" / "old.md", "# Old\n\nsourdough in the trash\n")
    write(
        wiki / "journal" / "2026" / "09" / "25" / "2026-09-25.md",
        "---\ndate: 2026-09-25\n---\n\nA quiet day. Some sourdough was baked.\n",
    )
    write(
        wiki / "journal" / "2026" / "09" / "25" / "alex-visit.md",
        "---\ndate: 2026-09-25\npeople: [alex]\nsource: agent\n---\n\nAlex came to visit.\n",
    )
    write(
        wiki / "journal" / "2026" / "09" / "25" / "garden-work.md",
        "---\ndate: 2026-09-25\npeople: []\n---\n\nThe garden got water.\n",
    )
    return data


@pytest.fixture
def wiki(data_dir: Path) -> Path:
    return data_dir / "wiki"


@pytest.fixture
def settings(data_dir: Path) -> Settings:
    return Settings(
        data_dir=data_dir,
        timezone=ZoneInfo("America/New_York"),
        tokens={
            WRITER: Caller(name="laptop"),
            READER: Caller(name="laptop-ci", readonly=True),
        },
    )


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    from joshua_mcp import server

    monkeypatch.setattr(server, "_now", lambda: FIXED_NOW)


@pytest.fixture
def app(settings: Settings) -> ASGIApp:
    from joshua_mcp.server import build_app

    return build_app(settings)


def snapshot(root: Path) -> dict[str, bytes]:
    """Every file under ``root`` and its bytes, to prove that nothing changed."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@contextlib.asynccontextmanager
async def mcp_session(app: ASGIApp, token: str | None = WRITER):
    """Open an MCP session to ``app`` at ``/mcp`` over an in-process ASGI transport.

    An ASGI transport never sends the ``lifespan`` protocol, so enter the
    session manager by hand, as the hello addon tests do.
    """
    from joshua_mcp.server import mcp as addon_mcp

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    transport = httpx2.ASGITransport(app=app)
    client = httpx2.AsyncClient(
        transport=transport, base_url="http://testserver", headers=headers, timeout=10
    )
    try:
        async with addon_mcp.session_manager.run():
            async with streamable_http_client("http://testserver/mcp", http_client=client) as (
                read,
                write_stream,
            ):
                async with ClientSession(read, write_stream) as session:
                    await session.initialize()
                    yield session
    finally:
        await client.aclose()
