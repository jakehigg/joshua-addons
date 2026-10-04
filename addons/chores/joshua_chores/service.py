"""The chores service: one implementation of each read and write.

The MCP tools and the REST API both call this module, so a rule (the
cooldown, the next due date, the ledger row of a completion) has one place.
Each function takes an ``AsyncSession`` as its first argument. Each write
function commits before it returns, and then reads attributes of the rows it
wrote. Use a session from ``database.build_sessionmaker``, which keeps those
attributes loaded after a commit (``expire_on_commit=False``).

The balance of a member is the sum of the ledger (``transactions.amount``).
No column stores it.

A function that cannot do its work raises a ``ChoresError``:
``NotFound`` for an id or slug that does not exist, ``Cooldown`` for a
completion that comes too soon after the last one, and ``InvalidArgument``
for a value that is not valid. The caller maps each one to its own error
(an MCP ``ToolError`` or an HTTP status).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import settings
from .models import FREQUENCIES, Chore, Completion, Member, Transaction
from .schedule import advance_to_current, next_due

SLUG_PATTERN = re.compile(r"[a-z0-9-]{1,32}")

# The fields that ``update_chore`` can change.
CHORE_FIELDS = frozenset({"name", "points", "frequency", "next_due_date", "member_id", "is_active"})


class ChoresError(Exception):
    """The base of each error that this module raises."""


class NotFound(ChoresError):
    """A member or a chore does not exist."""


class Cooldown(ChoresError):
    """A chore was completed too recently. ``retry_after`` is in seconds."""

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class InvalidArgument(ChoresError):
    """A value is not valid."""


@dataclass(frozen=True)
class ChoreView:
    """A chore as a list shows it.

    ``next_due_date`` is the due date of the current period (see
    ``schedule.advance_to_current``). It can be later than the stored date.
    """

    id: int
    member_id: int
    name: str
    points: int
    frequency: str
    is_active: bool
    next_due_date: date
    last_completed_at: datetime | None


@dataclass(frozen=True)
class CompletionResult:
    """The result of ``complete_chore``.

    ``next_due_date`` is ``None`` when the chore was a one-off and is now
    retired.
    """

    chore_id: int
    completion_id: int
    points_awarded: int
    balance: int
    next_due_date: date | None
    is_active: bool


@dataclass(frozen=True)
class LedgerResult:
    """The ledger row that ``award`` or ``deduct`` wrote, and the new balance."""

    transaction: Transaction
    balance: int


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _check_name(name: str, what: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise InvalidArgument(f"{what} must not be empty")
    return name.strip()


def _check_points(points: Any) -> int:
    if isinstance(points, bool) or not isinstance(points, int) or points <= 0:
        raise InvalidArgument(f"points must be a whole number more than 0, not {points!r}")
    return points


def _check_frequency(frequency: Any) -> str:
    if frequency not in FREQUENCIES:
        raise InvalidArgument(
            f"frequency must be one of {', '.join(FREQUENCIES)}, not {frequency!r}"
        )
    return frequency


def _check_date(value: Any, what: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise InvalidArgument(f"{what} must be a date, not {value!r}")
    return value


# Members


async def list_members(session: AsyncSession, include_inactive: bool = False) -> list[Member]:
    """Return the members in ``sort_order``, then in the order they were added.

    Inactive members are not in the list unless ``include_inactive`` is true.
    """
    query = select(Member).order_by(Member.sort_order, Member.id)
    if not include_inactive:
        query = query.where(Member.is_active.is_(True))
    return list((await session.scalars(query)).all())


async def get_member(session: AsyncSession, member_id: int) -> Member:
    """Return the member with ``member_id``, or raise ``NotFound``."""
    member = await session.get(Member, member_id)
    if member is None:
        raise NotFound(f"member {member_id} does not exist")
    return member


async def get_member_by_slug(session: AsyncSession, slug: str) -> Member:
    """Return the member with ``slug``, or raise ``NotFound``."""
    member = await session.scalar(select(Member).where(Member.slug == slug))
    if member is None:
        raise NotFound(f"member {slug!r} does not exist")
    return member


async def add_member(session: AsyncSession, slug: str, name: str) -> Member:
    """Add a member.

    ``slug`` is 1 to 32 characters of lowercase letters, digits, and
    hyphens, and no other member can have it. ``name`` must not be empty.
    """
    if not isinstance(slug, str) or not SLUG_PATTERN.fullmatch(slug):
        raise InvalidArgument(
            f"slug must be 1 to 32 lowercase letters, digits, or hyphens, not {slug!r}"
        )
    name = _check_name(name, "name")
    if await session.scalar(select(Member.id).where(Member.slug == slug)) is not None:
        raise InvalidArgument(f"member {slug!r} already exists")
    member = Member(slug=slug, name=name)
    session.add(member)
    await session.commit()
    return member


async def set_member(
    session: AsyncSession,
    slug: str,
    name: str | None = None,
    is_active: bool | None = None,
    sort_order: int | None = None,
) -> Member:
    """Change the name, the active flag, or the sort order of a member.

    An argument that is ``None`` does not change. The slug does not change.
    """
    member = await get_member_by_slug(session, slug)
    if name is not None:
        member.name = _check_name(name, "name")
    if is_active is not None:
        if not isinstance(is_active, bool):
            raise InvalidArgument(f"is_active must be true or false, not {is_active!r}")
        member.is_active = is_active
    if sort_order is not None:
        if isinstance(sort_order, bool) or not isinstance(sort_order, int):
            raise InvalidArgument(f"sort_order must be a whole number, not {sort_order!r}")
        member.sort_order = sort_order
    await session.commit()
    return member


# Balance and ledger


async def balance(session: AsyncSession, member_id: int) -> int:
    """Return the sum of the ledger of the member. A member with no rows has 0."""
    total = await session.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.member_id == member_id
        )
    )
    return int(total or 0)


async def ledger(session: AsyncSession, member_id: int, limit: int = 50) -> list[Transaction]:
    """Return the newest ``limit`` ledger rows of the member, newest first."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise InvalidArgument(f"limit must be a whole number more than 0, not {limit!r}")
    query = (
        select(Transaction)
        .where(Transaction.member_id == member_id)
        .order_by(Transaction.created_at.desc(), Transaction.id.desc())
        .limit(limit)
    )
    return list((await session.scalars(query)).all())


# Chores


async def get_chore(session: AsyncSession, chore_id: int) -> Chore:
    """Return the chore with ``chore_id``, active or retired, or raise ``NotFound``."""
    chore = await session.get(Chore, chore_id)
    if chore is None:
        raise NotFound(f"chore {chore_id} does not exist")
    return chore


async def list_chores(
    session: AsyncSession,
    member_id: int | None = None,
    overdue_only: bool = False,
    today: date | None = None,
) -> list[ChoreView]:
    """Return the active chores, sorted by due date, then by id.

    This function does not write. Each ``ChoreView.next_due_date`` is the
    stored date moved to the current period with
    ``schedule.advance_to_current``. The stored date changes only when a
    member completes the chore.

    ``member_id`` limits the list to the chores of one member.
    ``overdue_only`` limits it to the chores that are due on ``today`` or
    earlier. ``today`` defaults to the current UTC date.
    """
    if today is None:
        today = _utc_now().date()
    query = select(Chore).where(Chore.is_active.is_(True))
    if member_id is not None:
        query = query.where(Chore.member_id == member_id)
    views = [
        ChoreView(
            id=chore.id,
            member_id=chore.member_id,
            name=chore.name,
            points=chore.points,
            frequency=chore.frequency,
            is_active=chore.is_active,
            next_due_date=advance_to_current(chore.next_due_date, chore.frequency, today),
            last_completed_at=chore.last_completed_at,
        )
        for chore in (await session.scalars(query)).all()
    ]
    if overdue_only:
        views = [view for view in views if view.next_due_date <= today]
    views.sort(key=lambda view: (view.next_due_date, view.id))
    return views


async def add_chore(
    session: AsyncSession,
    member_id: int,
    name: str,
    points: int,
    frequency: str,
    next_due_date: date,
) -> Chore:
    """Add an active chore to an active member."""
    member = await get_member(session, member_id)
    if not member.is_active:
        raise InvalidArgument(f"member {member.slug!r} is not active")
    chore = Chore(
        member_id=member.id,
        name=_check_name(name, "name"),
        points=_check_points(points),
        frequency=_check_frequency(frequency),
        next_due_date=_check_date(next_due_date, "next_due_date"),
    )
    session.add(chore)
    await session.commit()
    return chore


async def update_chore(session: AsyncSession, chore_id: int, **fields: Any) -> Chore:
    """Change the fields of a chore.

    The fields are ``name``, ``points``, ``frequency``, ``next_due_date``,
    ``member_id``, and ``is_active``. A field that is ``None`` does not
    change. Another field name raises ``InvalidArgument``.
    """
    unknown = set(fields) - CHORE_FIELDS
    if unknown:
        raise InvalidArgument(f"cannot change {', '.join(sorted(unknown))}")
    chore = await get_chore(session, chore_id)
    checks = {
        "name": lambda value: _check_name(value, "name"),
        "points": _check_points,
        "frequency": _check_frequency,
        "next_due_date": lambda value: _check_date(value, "next_due_date"),
    }
    changes: dict[str, Any] = {}
    for field, value in fields.items():
        if value is None:
            continue
        if field in checks:
            changes[field] = checks[field](value)
        elif field == "member_id":
            changes[field] = (await get_member(session, value)).id
        elif not isinstance(value, bool):
            raise InvalidArgument(f"is_active must be true or false, not {value!r}")
        else:
            changes[field] = value
    for field, value in changes.items():
        setattr(chore, field, value)
    await session.commit()
    return chore


async def retire_chore(session: AsyncSession, chore_id: int) -> Chore:
    """Set ``is_active`` to false. The chore and its history stay in the database."""
    chore = await get_chore(session, chore_id)
    chore.is_active = False
    await session.commit()
    return chore


async def complete_chore(
    session: AsyncSession,
    chore_id: int,
    *,
    now: datetime | None = None,
    today: date | None = None,
    note: str | None = None,
    cooldown_seconds: int | None = None,
    actor: str | None = None,
) -> CompletionResult:
    """Record that the member of the chore completed it, and award its XP.

    The function writes one ``Completion`` row and one ledger row (source
    ``chore``, ``reference_id`` the completion id), and sets
    ``last_completed_at``. A one-off chore is then retired. A recurring
    chore gets the next due date after ``today``.

    A retired chore raises ``NotFound``. A second completion within
    ``cooldown_seconds`` of the last one raises ``Cooldown``.
    ``cooldown_seconds`` defaults to ``settings.cooldown_seconds()``, and 0
    turns the check off. ``now`` defaults to the current UTC time, and a
    naive ``now`` is UTC. The rows store ``now``. ``today`` is the date of
    the household (see ``settings.chores_tz``) and defaults to the UTC date
    of ``now``.
    """
    if now is None:
        now = _utc_now()
    elif now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if today is None:
        today = now.astimezone(UTC).date()
    if cooldown_seconds is None:
        cooldown_seconds = settings.cooldown_seconds()

    # On Postgres, lock the chore row until the commit, so two completions
    # at the same time cannot both pass the cooldown check. SQLite ignores
    # ``FOR UPDATE``.
    chore = await session.scalar(
        select(Chore)
        .where(Chore.id == chore_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if chore is None:
        raise NotFound(f"chore {chore_id} does not exist")
    if not chore.is_active:
        raise NotFound(f"chore {chore_id} is retired")

    if chore.last_completed_at is not None and cooldown_seconds > 0:
        elapsed = (now - chore.last_completed_at).total_seconds()
        if elapsed < cooldown_seconds:
            wait = max(1, math.ceil(cooldown_seconds - elapsed))
            raise Cooldown(f"chore {chore_id} was completed recently; wait {wait} s", wait)

    completion = Completion(
        chore_id=chore.id,
        member_id=chore.member_id,
        completed_at=now,
        points_awarded=chore.points,
        note=note,
    )
    session.add(completion)
    await session.flush()

    session.add(
        Transaction(
            member_id=chore.member_id,
            amount=chore.points,
            description=f"Completed: {chore.name}",
            source="chore",
            reference_id=completion.id,
            actor=actor,
            created_at=now,
        )
    )
    chore.last_completed_at = now
    if chore.frequency == "one_off":
        chore.is_active = False
        due: date | None = None
    else:
        due = next_due(chore.frequency, today)
        chore.next_due_date = due
    await session.commit()

    return CompletionResult(
        chore_id=chore.id,
        completion_id=completion.id,
        points_awarded=completion.points_awarded,
        balance=await balance(session, chore.member_id),
        next_due_date=due,
        is_active=chore.is_active,
    )


# Manager ledger writes


async def _write_ledger(
    session: AsyncSession,
    member_id: int,
    amount: int,
    description: str,
    source: str,
    actor: str | None,
) -> LedgerResult:
    member = await get_member(session, member_id)
    transaction = Transaction(
        member_id=member.id,
        amount=amount,
        description=_check_name(description, "description"),
        source=source,
        actor=actor,
    )
    session.add(transaction)
    await session.commit()
    return LedgerResult(transaction=transaction, balance=await balance(session, member.id))


async def award(
    session: AsyncSession,
    member_id: int,
    points: int,
    description: str,
    actor: str | None = None,
) -> LedgerResult:
    """Add ``points`` XP to the member (source ``one_off``). ``points`` must be more than 0."""
    points = _check_points(points)
    return await _write_ledger(session, member_id, points, description, "one_off", actor)


async def deduct(
    session: AsyncSession,
    member_id: int,
    points: int,
    description: str,
    actor: str | None = None,
) -> LedgerResult:
    """Remove ``points`` XP from the member (source ``withdrawal``).

    ``points`` must be more than 0. The ledger row has the amount
    ``-points``. The balance can go below 0.
    """
    points = _check_points(points)
    return await _write_ledger(session, member_id, -points, description, "withdrawal", actor)
