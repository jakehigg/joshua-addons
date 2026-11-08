"""``run_migrations`` runs on an empty model set, and is idempotent."""

from __future__ import annotations

from joshua_tasks.database import build_engine
from joshua_tasks.migrations import ADDITIVE_MIGRATIONS, run_migrations
from joshua_tasks.models import Base


async def test_run_migrations_is_idempotent(tmp_path) -> None:
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    try:
        await run_migrations(engine)
        await run_migrations(engine)
    finally:
        await engine.dispose()
    assert ADDITIVE_MIGRATIONS == []
    assert Base.metadata.tables == {}
