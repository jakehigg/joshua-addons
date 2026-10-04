"""complete_chore: XP, cooldown, schedule, and the ledger."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from joshua_chores import service
from joshua_chores.models import Completion, Transaction
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 6, 10, 18, 30, tzinfo=UTC)


async def test_complete_awards_xp_and_writes_the_ledger(
    session: AsyncSession, add_test_member
) -> None:
    member = await add_test_member()
    chore = await service.add_chore(session, member.id, "Dishes", 15, "daily", date(2026, 6, 1))

    result = await service.complete_chore(
        session, chore.id, now=NOW, note="done well", cooldown_seconds=60, actor="gateway"
    )

    assert result.chore_id == chore.id
    assert result.points_awarded == 15
    assert result.balance == 15
    assert result.is_active is True
    assert result.next_due_date == date(2026, 6, 11)

    completion = await session.get(Completion, result.completion_id)
    assert completion is not None
    assert completion.member_id == member.id
    assert completion.note == "done well"
    assert completion.completed_at == NOW

    [row] = (await session.scalars(select(Transaction))).all()
    assert row.amount == 15
    assert row.source == "chore"
    assert row.reference_id == completion.id
    assert row.actor == "gateway"
    assert row.description == "Completed: Dishes"

    stored = await service.get_chore(session, chore.id)
    assert stored.last_completed_at == NOW
    assert stored.next_due_date == date(2026, 6, 11)


@pytest.mark.parametrize(
    ("frequency", "expected"),
    [("daily", date(2026, 6, 11)), ("weekly", date(2026, 6, 17)), ("monthly", date(2026, 7, 10))],
)
async def test_recurring_chore_advances_from_the_completion_date(
    session: AsyncSession, add_test_member, frequency: str, expected: date
) -> None:
    member = await add_test_member()
    chore = await service.add_chore(session, member.id, "x", 1, frequency, date(2026, 1, 1))
    result = await service.complete_chore(session, chore.id, now=NOW, cooldown_seconds=0)
    assert result.next_due_date == expected
    assert result.is_active is True


async def test_one_off_retires_the_chore(session: AsyncSession, add_test_member) -> None:
    member = await add_test_member()
    chore = await service.add_chore(session, member.id, "x", 3, "one_off", date(2026, 6, 10))
    result = await service.complete_chore(session, chore.id, now=NOW, cooldown_seconds=0)
    assert result.is_active is False
    assert result.next_due_date is None
    stored = await service.get_chore(session, chore.id)
    assert stored.is_active is False
    assert stored.next_due_date == date(2026, 6, 10)
    with pytest.raises(service.NotFound, match="retired"):
        await service.complete_chore(session, chore.id, now=NOW + timedelta(days=1))


async def test_cooldown_raises_and_writes_nothing(session: AsyncSession, add_test_member) -> None:
    member = await add_test_member()
    chore = await service.add_chore(session, member.id, "x", 5, "daily", date(2026, 6, 10))
    await service.complete_chore(session, chore.id, now=NOW, cooldown_seconds=60)

    with pytest.raises(service.Cooldown) as raised:
        await service.complete_chore(
            session, chore.id, now=NOW + timedelta(seconds=20), cooldown_seconds=60
        )
    assert raised.value.retry_after == 40

    assert await session.scalar(select(func.count()).select_from(Completion)) == 1
    assert await service.balance(session, member.id) == 5

    result = await service.complete_chore(
        session, chore.id, now=NOW + timedelta(seconds=60), cooldown_seconds=60
    )
    assert result.balance == 10


async def test_cooldown_defaults_to_the_setting(
    session: AsyncSession, add_test_member, monkeypatch
) -> None:
    monkeypatch.setenv("COOLDOWN_SECONDS", "300")
    member = await add_test_member()
    chore = await service.add_chore(session, member.id, "x", 5, "daily", date(2026, 6, 10))
    await service.complete_chore(session, chore.id, now=NOW)
    with pytest.raises(service.Cooldown):
        await service.complete_chore(session, chore.id, now=NOW + timedelta(seconds=200))


async def test_cooldown_zero_turns_the_check_off(session: AsyncSession, add_test_member) -> None:
    member = await add_test_member()
    chore = await service.add_chore(session, member.id, "x", 2, "daily", date(2026, 6, 10))
    await service.complete_chore(session, chore.id, now=NOW, cooldown_seconds=0)
    result = await service.complete_chore(session, chore.id, now=NOW, cooldown_seconds=0)
    assert result.balance == 4


async def test_complete_unknown_chore_raises_not_found(session: AsyncSession) -> None:
    with pytest.raises(service.NotFound):
        await service.complete_chore(session, 999, now=NOW)


async def test_naive_now_is_utc_and_default_now_works(
    session: AsyncSession, add_test_member
) -> None:
    member = await add_test_member()
    chore = await service.add_chore(session, member.id, "x", 1, "daily", date(2026, 6, 10))
    await service.complete_chore(session, chore.id, now=NOW.replace(tzinfo=None))
    stored = await service.get_chore(session, chore.id)
    assert stored.last_completed_at == NOW

    other = await service.add_chore(session, member.id, "y", 1, "one_off", date(2026, 6, 10))
    result = await service.complete_chore(session, other.id)
    assert result.is_active is False


async def test_balance_equals_the_sum_of_the_ledger(session: AsyncSession, add_test_member) -> None:
    alpha = await add_test_member("alpha", "Alpha")
    beta = await add_test_member("beta", "Beta")
    chore = await service.add_chore(session, alpha.id, "x", 25, "daily", date(2026, 6, 10))
    for day in range(3):
        await service.complete_chore(
            session, chore.id, now=NOW + timedelta(days=day), cooldown_seconds=60
        )
    await service.award(session, alpha.id, 10, "bonus")
    await service.deduct(session, alpha.id, 30, "spent")
    await service.award(session, beta.id, 4, "bonus")

    total = await session.scalar(
        select(func.sum(Transaction.amount)).where(Transaction.member_id == alpha.id)
    )
    assert await service.balance(session, alpha.id) == total == 55
    assert await service.balance(session, beta.id) == 4
