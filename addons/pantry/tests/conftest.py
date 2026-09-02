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

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from joshua_pantry.database import build_engine, build_sessionmaker
from joshua_pantry.migrations import run_migrations
from joshua_pantry.models import Base
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

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
