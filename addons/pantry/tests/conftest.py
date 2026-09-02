"""Shared fixtures for the pantry data-layer tests.

Every data-layer test runs against a throwaway SQLite file by default. The
same tests also run against Postgres when ``TEST_DATABASE_URL`` is set (a
throwaway container, per the ticket's local proof step); those runs are
marked ``@pytest.mark.integration`` so they run only when the marker is
selected — the workspace root's ``addopts = "-m 'not integration'"`` skips
them by default, the same way ``../joshua-ai/core`` gates its Postgres suite
behind the ``integration`` marker in its own pytest config.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator

import httpx2
import pytest
import pytest_asyncio
from joshua_pantry.database import build_engine, build_sessionmaker
from joshua_pantry.migrations import run_migrations
from joshua_pantry.models import Base
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.types import ASGIApp

TEST_DATABASE_URL_ENV = "TEST_DATABASE_URL"


def _engine_params() -> list:
    params = [pytest.param("sqlite", id="sqlite")]
    if os.environ.get(TEST_DATABASE_URL_ENV):
        params.append(pytest.param("postgres", id="postgres", marks=pytest.mark.integration))
    return params


@pytest_asyncio.fixture(params=_engine_params())
async def engine(request: pytest.FixtureRequest, tmp_path) -> AsyncIterator[AsyncEngine]:
    """A migrated engine: a fresh SQLite file, or a clean Postgres schema."""
    if request.param == "postgres":
        eng = build_engine(os.environ[TEST_DATABASE_URL_ENV])
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        try:
            await run_migrations(eng)
            yield eng
        finally:
            async with eng.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
            await eng.dispose()
    else:
        db_path = tmp_path / "pantry.db"
        eng = build_engine(f"sqlite+aiosqlite:///{db_path}")
        try:
            await run_migrations(eng)
            yield eng
        finally:
            await eng.dispose()


@pytest_asyncio.fixture(params=_engine_params())
async def raw_engine(request: pytest.FixtureRequest, tmp_path) -> AsyncIterator[AsyncEngine]:
    """An engine with no schema applied yet, for testing ``run_migrations`` itself."""
    if request.param == "postgres":
        eng = build_engine(os.environ[TEST_DATABASE_URL_ENV])
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        try:
            yield eng
        finally:
            async with eng.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
            await eng.dispose()
    else:
        db_path = tmp_path / "pantry_raw.db"
        eng = build_engine(f"sqlite+aiosqlite:///{db_path}")
        try:
            yield eng
        finally:
            await eng.dispose()


@pytest_asyncio.fixture
async def sessionmaker_(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return build_sessionmaker(engine)


@pytest_asyncio.fixture
async def session(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sessionmaker_() as s:
        yield s


@pytest_asyncio.fixture
async def bound_db(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Bind ``joshua_pantry.server``'s module-level session factory to ``sessionmaker_``.

    Lets a test call a tool function directly (``server.record_purchase(...)``)
    without going through the server's lifespan, which is what normally opens
    this factory on startup.
    """
    import joshua_pantry.server as server_module

    previous = server_module._sessionmaker
    server_module._sessionmaker = sessionmaker_
    try:
        yield sessionmaker_
    finally:
        server_module._sessionmaker = previous


@pytest.fixture
def app_factory(monkeypatch, tmp_path):
    """Return a function that builds a fresh app, with ``ADDON_TOKEN`` and the
    database set as given. A ``db_path`` given twice points two apps at the
    same file, so a scenario can span two calls the way two receipts do.
    """

    def _build(token: str | None, db_path=None):
        if token is None:
            monkeypatch.delenv("ADDON_TOKEN", raising=False)
        else:
            monkeypatch.setenv("ADDON_TOKEN", token)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        path = db_path or (tmp_path / "pantry.db")
        monkeypatch.setenv("PANTRY_DB", str(path))

        from joshua_pantry.server import build_app

        return build_app()

    return _build


@contextlib.asynccontextmanager
async def mcp_session(app: ASGIApp, headers: dict[str, str] | None = None):
    """Open an MCP session to ``app`` at ``/mcp`` over an in-process ASGI transport.

    An ASGI transport never sends the ``lifespan`` protocol, so the session
    manager that ``build_app`` wires up through Starlette's lifespan never
    starts on its own. Enter it by hand — this is also what runs the pantry
    server's own lifespan (engine build + migrations), the same way
    joshua_gateway.upstream enters a real upstream's session manager.
    """
    from joshua_pantry.server import mcp as pantry_mcp

    transport = httpx2.ASGITransport(app=app)
    client = httpx2.AsyncClient(
        transport=transport, base_url="http://testserver", headers=headers or {}, timeout=10
    )
    try:
        async with pantry_mcp.session_manager.run():
            async with streamable_http_client("http://testserver/mcp", http_client=client) as (
                read,
                write,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
    finally:
        await client.aclose()
