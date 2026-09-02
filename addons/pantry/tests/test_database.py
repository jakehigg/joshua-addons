"""URL resolution: DATABASE_URL normalization, and the SQLite fallback."""

from __future__ import annotations

from pathlib import Path

from joshua_pantry.database import (
    DATABASE_URL_ENV,
    DEFAULT_SQLITE_PATH,
    PANTRY_DB_ENV,
    build_engine,
    resolve_database_url,
)


def test_default_sqlite_path_is_the_data_volume_convention() -> None:
    assert DEFAULT_SQLITE_PATH == "/data/pantry.db"


def test_bare_postgres_url_is_normalized_to_asyncpg(monkeypatch) -> None:
    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql://user:pw@host:5432/pantry")
    assert resolve_database_url() == "postgresql+asyncpg://user:pw@host:5432/pantry"


def test_asyncpg_url_passes_through_unchanged(monkeypatch) -> None:
    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql+asyncpg://user:pw@host:5432/pantry")
    assert resolve_database_url() == "postgresql+asyncpg://user:pw@host:5432/pantry"


def test_sqlite_used_when_database_url_unset(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    db_path = tmp_path / "nested" / "pantry.db"
    monkeypatch.setenv(PANTRY_DB_ENV, str(db_path))

    url = resolve_database_url()

    assert url == f"sqlite+aiosqlite:///{db_path}"
    assert db_path.parent.is_dir()  # the parent directory was created


def test_sqlite_parent_dir_created_when_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    db_path = tmp_path / "a" / "b" / "c" / "pantry.db"
    assert not db_path.parent.exists()
    monkeypatch.setenv(PANTRY_DB_ENV, str(db_path))

    resolve_database_url()

    assert db_path.parent.is_dir()


def test_build_engine_normalizes_an_explicit_bare_postgres_url() -> None:
    """A url passed straight to build_engine (e.g. TEST_DATABASE_URL in tests)
    gets the same asyncpg normalization as DATABASE_URL, not just the
    resolved default."""
    engine = build_engine("postgresql://user:pw@host:5432/pantry")
    assert engine.url.drivername == "postgresql+asyncpg"


def test_build_engine_passes_through_sqlite_url(tmp_path: Path) -> None:
    db_path = tmp_path / "explicit.db"
    engine = build_engine(f"sqlite+aiosqlite:///{db_path}")
    assert engine.url.drivername == "sqlite+aiosqlite"
