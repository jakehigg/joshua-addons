"""Shared fixtures for the vinyl addon tests.

Every test runs offline. The Discogs and MusicBrainz responses are real
captures in ``fixtures/``, chosen for variety: a solo artist, a band with a
leading ``The``, a compilation, a soundtrack by one composer, a classical
release credited composer-first, a country record whose Discogs genre is
``Folk, World, & Country``, and one release with no styles at all.

The MCP tests drive the ASGI app in-process, the same way the hello addon
does, over ``httpx2.ASGITransport``.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from joshua_vinyl import db
from joshua_vinyl.config import ShelfConfig
from joshua_vinyl.musicbrainz import ArtistSort, best_match
from joshua_vinyl.sync import SyncResult, run_sync
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.types import ASGIApp

FIXTURES = Path(__file__).parent / "fixtures"

RELEASE_SLUGS = (
    "dylan_highway61",
    "beatles_abbey_road",
    "various_nuggets",
    "morricone_good_bad_ugly",
    "gould_goldberg",
    "cash_folsom",
)


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def collection_items() -> list[dict[str, Any]]:
    """The four real collection items, plus the six varied releases wrapped as items."""
    items = list(load_fixture("discogs_collection_page1.json")["releases"])
    for n, slug in enumerate(RELEASE_SLUGS, start=1):
        release = load_fixture(f"discogs_release_{slug}.json")
        items.append(
            {
                "id": release["id"],
                "instance_id": 900_000 + n,
                "date_added": f"2026-01-{n:02d}T12:00:00-07:00",
                "basic_information": release,
            }
        )
    return items


class FakeDiscogs:
    """Serves the fixture items and fake image bytes. Counts every download."""

    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self.items = collection_items() if items is None else items
        self.downloads: list[str] = []
        self.fail_urls: set[str] = set()

    def collection(self, username: str) -> Iterator[dict[str, Any]]:
        yield from self.items

    def download(self, url: str) -> bytes:
        self.downloads.append(url)
        if url in self.fail_urls:
            raise RuntimeError("download failed")
        return b"\xff\xd8fake-jpeg"


class FakeMusicBrainz:
    """Answers from the captured artist searches. Counts every lookup."""

    def __init__(self) -> None:
        self.lookups: list[str] = []

    def search_artist(self, name: str) -> ArtistSort | None:
        self.lookups.append(name)
        slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        path = FIXTURES / f"musicbrainz_artist_{slug}.json"
        if not path.is_file():
            return None
        return best_match(name, load_fixture(path.name)["artists"])


@pytest.fixture
def fake_discogs() -> FakeDiscogs:
    return FakeDiscogs()


@pytest.fixture
def fake_musicbrainz() -> FakeMusicBrainz:
    return FakeMusicBrainz()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    return path


def sync_into(
    data_dir: Path,
    discogs: FakeDiscogs,
    musicbrainz: FakeMusicBrainz | None,
    config: ShelfConfig | None = None,
) -> SyncResult:
    conn = db.connect(data_dir / "vinyl.db")
    try:
        return run_sync(
            conn,
            discogs=discogs,
            musicbrainz=musicbrainz,
            username="example-user",
            config=config or ShelfConfig(),
            art_dir=data_dir / "art",
            bundle_dir=data_dir / "bundle",
        )
    finally:
        conn.close()


@pytest.fixture
def synced(data_dir: Path, fake_discogs: FakeDiscogs, fake_musicbrainz: FakeMusicBrainz) -> Path:
    """A data directory after one full sync from the fixtures."""
    sync_into(data_dir, fake_discogs, fake_musicbrainz)
    return data_dir


@pytest.fixture
def app_factory(monkeypatch, data_dir: Path):
    """Build a fresh app with ``VINYL_DATA_DIR`` at ``data_dir`` and ``ADDON_TOKEN`` as given."""

    def _build(token: str | None, **env: str) -> ASGIApp:
        monkeypatch.setenv("VINYL_DATA_DIR", str(data_dir))
        monkeypatch.delenv("VINYL_STATIC_DIR", raising=False)
        monkeypatch.delenv("DISCOGS_TOKEN", raising=False)
        if token is None:
            monkeypatch.delenv("ADDON_TOKEN", raising=False)
        else:
            monkeypatch.setenv("ADDON_TOKEN", token)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        from joshua_vinyl.server import build_app

        return build_app()

    return _build


@contextlib.asynccontextmanager
async def mcp_session(app: ASGIApp, headers: dict[str, str] | None = None):
    """Open an MCP session to ``app`` at ``/mcp`` over an in-process ASGI transport."""
    from joshua_vinyl.server import mcp as vinyl_mcp

    transport = httpx2.ASGITransport(app=app)
    client = httpx2.AsyncClient(
        transport=transport, base_url="http://testserver", headers=headers or {}, timeout=10
    )
    try:
        async with vinyl_mcp.session_manager.run():
            async with streamable_http_client("http://testserver/mcp", http_client=client) as (
                read,
                write,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
    finally:
        await client.aclose()
