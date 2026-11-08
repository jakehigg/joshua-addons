"""Member functions of the service."""

from __future__ import annotations

import pytest
from joshua_chores import service
from sqlalchemy.ext.asyncio import AsyncSession


async def test_add_member_and_get_it_by_slug(session: AsyncSession) -> None:
    member = await service.add_member(session, "alpha-2", "  Alpha  ")
    assert member.id is not None
    assert member.name == "Alpha"
    found = await service.get_member_by_slug(session, "alpha-2")
    assert found.id == member.id
    assert (await service.get_member(session, member.id)).slug == "alpha-2"


@pytest.mark.parametrize("slug", ["", "Alpha", "a b", "a_b", "x" * 33, "é"])
async def test_add_member_rejects_a_bad_slug(session: AsyncSession, slug: str) -> None:
    with pytest.raises(service.InvalidArgument, match="slug"):
        await service.add_member(session, slug, "Name")


async def test_add_member_accepts_a_32_character_slug(session: AsyncSession) -> None:
    await service.add_member(session, "a" * 32, "Name")


async def test_add_member_rejects_a_duplicate_slug(session: AsyncSession) -> None:
    await service.add_member(session, "alpha", "Alpha")
    with pytest.raises(service.InvalidArgument, match="already exists"):
        await service.add_member(session, "alpha", "Other")


async def test_add_member_rejects_an_empty_name(session: AsyncSession) -> None:
    with pytest.raises(service.InvalidArgument, match="name"):
        await service.add_member(session, "alpha", "   ")


async def test_unknown_member_raises_not_found(session: AsyncSession) -> None:
    with pytest.raises(service.NotFound):
        await service.get_member_by_slug(session, "nobody")
    with pytest.raises(service.NotFound):
        await service.get_member(session, 12345)
    with pytest.raises(service.NotFound):
        await service.set_member(session, "nobody", name="X")


async def test_list_members_sorts_and_hides_inactive(session: AsyncSession) -> None:
    await service.add_member(session, "first", "First")
    await service.add_member(session, "second", "Second")
    await service.add_member(session, "third", "Third")
    await service.set_member(session, "first", sort_order=5)
    await service.set_member(session, "second", is_active=False)

    active = await service.list_members(session)
    assert [m.slug for m in active] == ["third", "first"]

    every = await service.list_members(session, include_inactive=True)
    assert [m.slug for m in every] == ["second", "third", "first"]


async def test_set_member_changes_only_the_given_fields(session: AsyncSession) -> None:
    await service.add_member(session, "alpha", "Alpha")
    member = await service.set_member(session, "alpha", name="Renamed")
    assert member.name == "Renamed"
    assert member.is_active is True
    assert member.sort_order == 0
    member = await service.set_member(session, "alpha", is_active=False, sort_order=-1)
    assert member.name == "Renamed"
    assert member.is_active is False
    assert member.sort_order == -1


@pytest.mark.parametrize(
    "kwargs",
    [{"name": ""}, {"is_active": "yes"}, {"sort_order": "1"}, {"sort_order": True}],
)
async def test_set_member_rejects_bad_values(session: AsyncSession, kwargs: dict) -> None:
    await service.add_member(session, "alpha", "Alpha")
    with pytest.raises(service.InvalidArgument):
        await service.set_member(session, "alpha", **kwargs)


def test_errors_share_one_base() -> None:
    for error in (service.NotFound, service.Cooldown, service.InvalidArgument):
        assert issubclass(error, service.ChoresError)
