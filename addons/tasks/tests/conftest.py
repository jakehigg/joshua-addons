"""Shared fixtures for the tasks addon tests."""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator

import httpx2
import pytest
import pytest_asyncio
from joshua_tasks.database import build_engine, build_sessionmaker
from joshua_tasks.migrations import run_migrations
from joshua_tasks.models import Base
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.types import ASGIApp

TEST_DATABASE_URL_ENV = "TEST_DATABASE_URL"


def _engine_params() -> list:
    """SQLite always; Postgres too when ``TEST_DATABASE_URL`` is set.

    The Postgres runs carry the ``integration`` marker, so the workspace's
    ``-m 'not integration'`` default skips them. Select them with
    ``TEST_DATABASE_URL=... pytest -m integration`` (or ``-m ''`` for both).
    """
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
        eng = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'tasks.db'}")
        try:
            await run_migrations(eng)
            yield eng
        finally:
            await eng.dispose()


@pytest_asyncio.fixture
async def sessionmaker_(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return build_sessionmaker(engine)


@pytest_asyncio.fixture
async def db(sessionmaker_: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with sessionmaker_() as s:
        yield s


@pytest_asyncio.fixture
async def bound_db(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Bind ``joshua_tasks.server``'s module-level session factory to ``sessionmaker_``.

    Lets a test call a tool function or a REST route without the server's
    lifespan, which is what normally opens this factory on startup.
    """
    import joshua_tasks.server as server_module

    previous = server_module._sessionmaker
    server_module._sessionmaker = sessionmaker_
    try:
        yield sessionmaker_
    finally:
        server_module._sessionmaker = previous


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
