"""Table constraints: CHECK on frequency and source, and the completion cascade."""

from __future__ import annotations

from datetime import date

import pytest
from joshua_chores.models import Chore, Completion, Member, Transaction
from sqlalchemy import delete, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


async def _member(session: AsyncSession) -> Member:
    member = Member(slug="alpha", name="Alpha")
    session.add(member)
    await session.commit()
    return member


async def test_member_defaults(session: AsyncSession) -> None:
    member = await _member(session)
    assert member.is_active is True
    assert member.sort_order == 0
    assert member.pin_hash is None
    assert member.created_at.utcoffset() is not None


async def test_member_slug_is_unique(session: AsyncSession) -> None:
    await _member(session)
    session.add(Member(slug="alpha", name="Other"))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_check_constraint_rejects_a_bad_frequency(session: AsyncSession) -> None:
    member = await _member(session)
    session.add(
        Chore(
            member_id=member.id,
            name="x",
            points=1,
            frequency="hourly",
            next_due_date=date(2026, 1, 1),
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_check_constraint_rejects_a_bad_source(session: AsyncSession) -> None:
    member = await _member(session)
    session.add(Transaction(member_id=member.id, amount=1, description="x", source="gift"))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_each_documented_source_is_accepted(session: AsyncSession) -> None:
    member = await _member(session)
    for source in ("chore", "one_off", "withdrawal", "adjustment"):
        session.add(Transaction(member_id=member.id, amount=1, description="x", source=source))
    await session.commit()


async def test_chore_member_must_exist(session: AsyncSession) -> None:
    session.add(
        Chore(member_id=999, name="x", points=1, frequency="daily", next_due_date=date(2026, 1, 1))
    )
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_deleting_a_chore_deletes_its_completions(session: AsyncSession) -> None:
    member = await _member(session)
    chore = Chore(
        member_id=member.id, name="x", points=1, frequency="daily", next_due_date=date(2026, 1, 1)
    )
    session.add(chore)
    await session.flush()
    session.add(Completion(chore_id=chore.id, member_id=member.id, points_awarded=1))
    await session.commit()

    await session.execute(delete(Chore).where(Chore.id == chore.id))
    await session.commit()

    assert await session.scalar(select(func.count()).select_from(Completion)) == 0


async def test_transactions_index_on_member_and_time(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        indexes = await conn.run_sync(
            lambda sync_conn: inspect(sync_conn).get_indexes("transactions")
        )
    assert any(index["column_names"] == ["member_id", "created_at"] for index in indexes)


async def test_no_table_stores_a_balance(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        columns = await conn.run_sync(
            lambda sync_conn: {col["name"] for col in inspect(sync_conn).get_columns("members")}
        )
    assert not any("balance" in column for column in columns)


async def test_no_member_rows_after_migration(session: AsyncSession) -> None:
    assert await session.scalar(select(func.count()).select_from(Member)) == 0
