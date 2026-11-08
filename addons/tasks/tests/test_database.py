"""URL resolution: DATABASE_URL normalization, and the SQLite fallback."""

from __future__ import annotations

from pathlib import Path

from joshua_tasks.database import (
    DATABASE_URL_ENV,
    DEFAULT_SQLITE_PATH,
    TASKS_DB_ENV,
    build_engine,
    resolve_database_url,
)
from sqlalchemy import text


def test_default_sqlite_path_is_the_data_volume_convention() -> None:
    assert DEFAULT_SQLITE_PATH == "/data/tasks.db"


def test_bare_postgres_url_is_normalized_to_asyncpg(monkeypatch) -> None:
    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql://user:pw@host:5432/tasks")
    assert resolve_database_url() == "postgresql+asyncpg://user:pw@host:5432/tasks"


def test_asyncpg_url_passes_through_unchanged(monkeypatch) -> None:
    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql+asyncpg://user:pw@host:5432/tasks")
    assert resolve_database_url() == "postgresql+asyncpg://user:pw@host:5432/tasks"


def test_sqlite_used_when_database_url_unset(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    db_path = tmp_path / "nested" / "tasks.db"
    monkeypatch.setenv(TASKS_DB_ENV, str(db_path))

    assert resolve_database_url() == f"sqlite+aiosqlite:///{db_path}"
    assert db_path.parent.is_dir()


def test_build_engine_normalizes_an_explicit_bare_postgres_url() -> None:
    assert build_engine("postgresql://u:p@h:5432/t").url.drivername == "postgresql+asyncpg"


async def test_sqlite_engine_enforces_foreign_keys(tmp_path: Path) -> None:
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'fk.db'}")
    try:
        async with engine.connect() as conn:
            value = (await conn.execute(text("PRAGMA foreign_keys"))).scalar_one()
        assert value == 1
    finally:
        await engine.dispose()
