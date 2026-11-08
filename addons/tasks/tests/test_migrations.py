"""``run_migrations`` creates the three tables, and is idempotent."""

from __future__ import annotations

from joshua_tasks.database import build_engine
from joshua_tasks.migrations import ADDITIVE_MIGRATIONS, run_migrations
from joshua_tasks.models import Base
from sqlalchemy import inspect


async def test_run_migrations_is_idempotent(tmp_path) -> None:
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    try:
        await run_migrations(engine)
        await run_migrations(engine)
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
    finally:
        await engine.dispose()
    assert ADDITIVE_MIGRATIONS == []
    assert set(Base.metadata.tables) == {"projects", "groups", "tasks"}
    assert tables == {"projects", "groups", "tasks"}
