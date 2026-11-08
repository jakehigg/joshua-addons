"""Chore functions of the service: list, add, update, retire."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from joshua_chores import service
from joshua_chores.models import Chore
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TODAY = date(2026, 6, 10)


async def _stored_due_dates(sessionmaker_: async_sessionmaker[AsyncSession]) -> dict[int, date]:
    async with sessionmaker_() as fresh:
        rows = await fresh.execute(select(Chore.id, Chore.next_due_date))
        return {row.id: row.next_due_date for row in rows}


async def test_add_chore(session: AsyncSession, add_test_member) -> None:
    member = await add_test_member()
    chore = await service.add_chore(session, member.id, " Dishes ", 10, "daily", TODAY)
    assert chore.id is not None
    assert chore.name == "Dishes"
    assert chore.is_active is True
    assert chore.last_completed_at is None
    assert chore.next_due_date == TODAY


@pytest.mark.parametrize(
    ("name", "points", "frequency", "due", "match"),
    [
        ("", 10, "daily", TODAY, "name"),
        ("x", 0, "daily", TODAY, "points"),
        ("x", -5, "daily", TODAY, "points"),
        ("x", True, "daily", TODAY, "points"),
        ("x", 1.5, "daily", TODAY, "points"),
        ("x", 10, "hourly", TODAY, "frequency"),
        ("x", 10, "daily", "2026-06-10", "next_due_date"),
    ],
)
async def test_add_chore_rejects_bad_values(
    session: AsyncSession, add_test_member, name, points, frequency, due, match
) -> None:
    member = await add_test_member()
    with pytest.raises(service.InvalidArgument, match=match):
        await service.add_chore(session, member.id, name, points, frequency, due)


async def test_add_chore_needs_an_active_member(session: AsyncSession, add_test_member) -> None:
    with pytest.raises(service.NotFound):
        await service.add_chore(session, 999, "x", 1, "daily", TODAY)
    await add_test_member()
    await service.set_member(session, "alpha", is_active=False)
    member = await service.get_member_by_slug(session, "alpha")
    with pytest.raises(service.InvalidArgument, match="not active"):
        await service.add_chore(session, member.id, "x", 1, "daily", TODAY)


async def test_list_never_writes(
    session: AsyncSession, sessionmaker_: async_sessionmaker[AsyncSession], add_test_member
) -> None:
    member = await add_test_member()
    stale = TODAY - timedelta(days=40)
    for frequency in ("daily", "weekly", "monthly", "one_off"):
        await service.add_chore(session, member.id, frequency, 5, frequency, stale)
    before = await _stored_due_dates(sessionmaker_)

    first = await service.list_chores(session, today=TODAY)
    await session.commit()
    second = await service.list_chores(session, today=TODAY)
    await session.commit()

    assert await _stored_due_dates(sessionmaker_) == before
    assert set(before.values()) == {stale}
    assert first == second
    by_name = {view.name: view.next_due_date for view in first}
    assert by_name["daily"] == TODAY
    assert by_name["one_off"] == stale
    assert by_name["weekly"] >= TODAY
    assert by_name["monthly"] >= TODAY


async def test_list_sorts_by_due_date_and_filters_by_member(
    session: AsyncSession, add_test_member
) -> None:
    alpha = await add_test_member("alpha", "Alpha")
    beta = await add_test_member("beta", "Beta")
    late = await service.add_chore(session, alpha.id, "late", 1, "weekly", TODAY + timedelta(3))
    soon = await service.add_chore(session, alpha.id, "soon", 1, "weekly", TODAY + timedelta(1))
    other = await service.add_chore(session, beta.id, "other", 1, "daily", TODAY)

    views = await service.list_chores(session, today=TODAY)
    assert [view.id for view in views] == [other.id, soon.id, late.id]

    alpha_views = await service.list_chores(session, member_id=alpha.id, today=TODAY)
    assert [view.id for view in alpha_views] == [soon.id, late.id]


async def test_list_hides_retired_chores(session: AsyncSession, add_test_member) -> None:
    member = await add_test_member()
    chore = await service.add_chore(session, member.id, "x", 1, "daily", TODAY)
    retired = await service.retire_chore(session, chore.id)
    assert retired.is_active is False
    assert await service.list_chores(session, today=TODAY) == []
    assert (await service.get_chore(session, chore.id)).is_active is False


async def test_overdue_only(session: AsyncSession, add_test_member) -> None:
    member = await add_test_member()
    overdue = await service.add_chore(session, member.id, "old", 1, "daily", TODAY - timedelta(5))
    due_today = await service.add_chore(session, member.id, "now", 1, "one_off", TODAY)
    await service.add_chore(session, member.id, "later", 1, "daily", TODAY + timedelta(1))
    # Weekly, missed: the current period starts after today, so it is not due.
    await service.add_chore(session, member.id, "skipped", 1, "weekly", TODAY - timedelta(2))

    views = await service.list_chores(session, overdue_only=True, today=TODAY)
    assert {view.id for view in views} == {overdue.id, due_today.id}


async def test_list_defaults_today_to_the_current_date(
    session: AsyncSession, add_test_member
) -> None:
    member = await add_test_member()
    await service.add_chore(session, member.id, "x", 1, "daily", date(2000, 1, 1))
    [view] = await service.list_chores(session)
    assert view.next_due_date > date(2000, 1, 1)


async def test_update_chore(session: AsyncSession, add_test_member) -> None:
    alpha = await add_test_member("alpha", "Alpha")
    beta = await add_test_member("beta", "Beta")
    chore = await service.add_chore(session, alpha.id, "x", 1, "daily", TODAY)

    updated = await service.update_chore(
        session,
        chore.id,
        name="y",
        points=7,
        frequency="monthly",
        next_due_date=TODAY + timedelta(2),
        member_id=beta.id,
        is_active=None,
    )
    assert (updated.name, updated.points, updated.frequency) == ("y", 7, "monthly")
    assert updated.next_due_date == TODAY + timedelta(2)
    assert updated.member_id == beta.id
    assert updated.is_active is True

    await service.retire_chore(session, chore.id)
    revived = await service.update_chore(session, chore.id, is_active=True)
    assert revived.is_active is True


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ({"points": 0}, service.InvalidArgument),
        ({"frequency": "yearly"}, service.InvalidArgument),
        ({"is_active": 1}, service.InvalidArgument),
        ({"last_completed_at": None}, service.InvalidArgument),
        ({"member_id": 999}, service.NotFound),
    ],
)
async def test_update_chore_rejects_bad_fields(
    session: AsyncSession, add_test_member, fields, error
) -> None:
    member = await add_test_member()
    chore = await service.add_chore(session, member.id, "x", 1, "daily", TODAY)
    with pytest.raises(error):
        await service.update_chore(session, chore.id, name="changed", **fields)
    assert (await service.get_chore(session, chore.id)).name == "x"


async def test_unknown_chore_raises_not_found(session: AsyncSession) -> None:
    with pytest.raises(service.NotFound):
        await service.update_chore(session, 999, name="x")
    with pytest.raises(service.NotFound):
        await service.retire_chore(session, 999)
