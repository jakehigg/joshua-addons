"""Database engine and session factory for the pantry addon.

One engine-neutral code path for both dialects: Postgres when ``DATABASE_URL``
is set (a bare ``postgresql://`` URL is normalized to the asyncpg driver so a
value copied from `psql` or another tool still works), SQLite otherwise, at
``PANTRY_DB`` (default ``/data/pantry.db``; its parent directory is created if
missing). No SQL in this module is dialect-specific.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

DATABASE_URL_ENV = "DATABASE_URL"
PANTRY_DB_ENV = "PANTRY_DB"
DEFAULT_SQLITE_PATH = "/data/pantry.db"


def _normalize_postgres_url(url: str) -> str:
    """Force the asyncpg driver onto a bare ``postgresql://`` URL."""
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


def resolve_database_url() -> str:
    """Return the URL to connect with.

    ``DATABASE_URL`` wins when set. Otherwise, SQLite at ``PANTRY_DB``
    (default ``/data/pantry.db``); the parent directory is created so a fresh
    volume mount does not need to exist ahead of time.
    """
    url = os.environ.get(DATABASE_URL_ENV)
    if url:
        return _normalize_postgres_url(url)

    path = Path(os.environ.get(PANTRY_DB_ENV, DEFAULT_SQLITE_PATH))
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{path}"


def build_engine(url: str | None = None) -> AsyncEngine:
    """Build the async engine for ``url``, or the resolved default when omitted.

    A ``url`` given explicitly (a test's ``TEST_DATABASE_URL``, for example)
    is normalized the same way ``resolve_database_url`` normalizes
    ``DATABASE_URL``, so a bare ``postgresql://`` value works here too.

    SQLite does not enforce foreign keys unless a connection turns it on, so
    ``ON DELETE SET NULL``/``CASCADE`` (see models.py) would silently no-op on
    SQLite while working on Postgres. Turning the pragma on for every SQLite
    connection keeps that behavior engine-neutral.
    """
    resolved = _normalize_postgres_url(url) if url else resolve_database_url()
    engine = create_async_engine(resolved, echo=False, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine.sync_engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build a session factory bound to ``engine``."""
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Open one session from ``sessionmaker`` and close it on exit."""
    async with sessionmaker() as session:
        yield session
