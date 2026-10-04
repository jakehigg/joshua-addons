"""Shared fixtures for the chores tests.

Each data test runs against a new SQLite file by default. When
``TEST_DATABASE_URL`` is set, the same tests also run against Postgres. Those
runs have the ``integration`` marker, and the workspace ``addopts`` skips that
marker by default.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator

import httpx2
import pytest
import pytest_asyncio
from joshua_chores.database import build_engine, build_sessionmaker
from joshua_chores.migrations import run_migrations
from joshua_chores.models import Base
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
    """A migrated engine: a new SQLite file, or a clean Postgres schema."""
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
        db_path = tmp_path / "chores.db"
        eng = build_engine(f"sqlite+aiosqlite:///{db_path}")
        try:
            await run_migrations(eng)
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


@pytest.fixture
def add_test_member(session: AsyncSession):
    """Return a function that adds a member with ``slug`` through the service."""
    from joshua_chores import service

    async def _add(slug: str = "alpha", name: str = "Alpha") -> service.Member:
        return await service.add_member(session, slug, name)

    return _add


@pytest_asyncio.fixture(params=_engine_params())
async def raw_engine(request: pytest.FixtureRequest, tmp_path) -> AsyncIterator[AsyncEngine]:
    """An engine with no schema yet, to test ``run_migrations`` itself."""
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
        db_path = tmp_path / "chores_raw.db"
        eng = build_engine(f"sqlite+aiosqlite:///{db_path}")
        try:
            yield eng
        finally:
            await eng.dispose()


@pytest.fixture
def app_factory(monkeypatch, tmp_path):
    """Return a function that builds a new app with ``ADDON_TOKEN`` and the database set."""

    def _build(token: str | None, db_path=None):
        if token is None:
            monkeypatch.delenv("ADDON_TOKEN", raising=False)
        else:
            monkeypatch.setenv("ADDON_TOKEN", token)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        path = db_path or (tmp_path / "chores.db")
        monkeypatch.setenv("CHORES_DB", str(path))

        from joshua_chores.server import build_app

        return build_app()

    return _build


@contextlib.asynccontextmanager
async def mcp_session(app: ASGIApp, headers: dict[str, str] | None = None):
    """Open an MCP session to ``app`` at ``/mcp`` over an in-process ASGI transport.

    An ASGI transport does not send the ``lifespan`` protocol, so the session
    manager that ``build_app`` connects to the Starlette lifespan does not
    start by itself. This fixture starts it by hand. That also runs the
    lifespan of the chores server (engine build and migrations).
    """
    from joshua_chores.server import mcp as chores_mcp

    transport = httpx2.ASGITransport(app=app)
    client = httpx2.AsyncClient(
        transport=transport, base_url="http://testserver", headers=headers or {}, timeout=10
    )
    try:
        async with chores_mcp.session_manager.run():
            async with streamable_http_client("http://testserver/mcp", http_client=client) as (
                read,
                write,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
    finally:
        await client.aclose()
