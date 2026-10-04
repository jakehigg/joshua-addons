"""award, deduct, balance, and ledger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from joshua_chores import service
from joshua_chores.models import Transaction
from sqlalchemy.ext.asyncio import AsyncSession


async def test_balance_of_a_member_with_no_rows_is_zero(
    session: AsyncSession, add_test_member
) -> None:
    member = await add_test_member()
    assert await service.balance(session, member.id) == 0
    assert await service.ledger(session, member.id) == []


async def test_award_writes_a_positive_one_off_row(session: AsyncSession, add_test_member) -> None:
    member = await add_test_member()
    result = await service.award(session, member.id, 40, " Helped out ", actor="manager")
    assert result.balance == 40
    row = result.transaction
    assert row.id is not None
    assert row.amount == 40
    assert row.source == "one_off"
    assert row.description == "Helped out"
    assert row.actor == "manager"
    assert row.reference_id is None


async def test_deduct_writes_a_negative_withdrawal_row(
    session: AsyncSession, add_test_member
) -> None:
    member = await add_test_member()
    await service.award(session, member.id, 40, "start")
    result = await service.deduct(session, member.id, 15, "Toy")
    assert result.transaction.amount == -15
    assert result.transaction.source == "withdrawal"
    assert result.transaction.actor is None
    assert result.balance == 25


async def test_deduct_can_go_below_zero(session: AsyncSession, add_test_member) -> None:
    member = await add_test_member()
    result = await service.deduct(session, member.id, 5, "advance")
    assert result.balance == -5


@pytest.mark.parametrize("points", [0, -10, True, 2.5, "10"])
async def test_award_and_deduct_refuse_points_that_are_not_positive(
    session: AsyncSession, add_test_member, points
) -> None:
    member = await add_test_member()
    with pytest.raises(service.InvalidArgument, match="points"):
        await service.award(session, member.id, points, "x")
    with pytest.raises(service.InvalidArgument, match="points"):
        await service.deduct(session, member.id, points, "x")
    assert await service.balance(session, member.id) == 0


async def test_award_needs_a_description_and_a_member(
    session: AsyncSession, add_test_member
) -> None:
    member = await add_test_member()
    with pytest.raises(service.InvalidArgument, match="description"):
        await service.award(session, member.id, 5, "  ")
    with pytest.raises(service.NotFound):
        await service.deduct(session, 999, 5, "x")


async def test_ledger_is_newest_first_and_limited(session: AsyncSession, add_test_member) -> None:
    member = await add_test_member()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(5):
        session.add(
            Transaction(
                member_id=member.id,
                amount=index + 1,
                description=f"row {index}",
                source="adjustment",
                created_at=start + timedelta(hours=index),
            )
        )
    await session.commit()

    rows = await service.ledger(session, member.id, limit=3)
    assert [row.description for row in rows] == ["row 4", "row 3", "row 2"]
    assert len(await service.ledger(session, member.id)) == 5


async def test_ledger_of_no_member_gives_the_rows_of_all_members(
    session: AsyncSession, add_test_member
) -> None:
    alpha = await add_test_member("alpha", "Alpha")
    beta = await add_test_member("beta", "Beta")
    await service.award(session, alpha.id, 5, "first")
    await service.award(session, beta.id, 7, "second")

    rows = await service.ledger(session, None)
    assert {row.member_id for row in rows} == {alpha.id, beta.id}
    assert [row.description for row in await service.ledger(session, beta.id)] == ["second"]


@pytest.mark.parametrize("limit", [0, -1, True])
async def test_ledger_refuses_a_bad_limit(session: AsyncSession, limit) -> None:
    with pytest.raises(service.InvalidArgument, match="limit"):
        await service.ledger(session, 1, limit=limit)
