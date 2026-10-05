"""Bulk import of members, chores, completions, and ledger rows.

``docs/import.md`` gives the file format. ``import_data`` checks the full
batch before it writes, and then writes it in one transaction. A second run
of the same batch changes nothing:

- A member matches on ``slug``.
- A chore matches on ``external_id``, or on the member, the name, and
  ``created_at``. When the row has no ``created_at``, it matches on the
  member and the name.
- A completion and a ledger row match on ``external_id``.

A row that matches is skipped. Import does not change a row that exists.
Each imported row keeps the timestamps of the file. The addon stores the
``external_id`` in the ``import_id`` column.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StringConstraints,
    ValidationError,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import FREQUENCIES, SOURCES, Chore, Completion, Member, Transaction
from .service import SLUG_PATTERN, InvalidArgument, balance

FORMAT_VERSION = 1

# The value of ``transactions.actor`` on each imported ledger row.
IMPORT_ACTOR = "import"

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Slug = Annotated[str, StringConstraints(pattern=f"^{SLUG_PATTERN.pattern}$")]


def _utc(value: datetime | None) -> datetime | None:
    """Return ``value`` in UTC. A naive value is UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class _Row(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The position of the row in its section of the batch, for the errors.
    _index: int = PrivateAttr(default=0)


class MemberRow(_Row):
    slug: Slug
    name: Text
    is_active: bool = True
    sort_order: int = 0


class ChoreRow(_Row):
    member: Slug
    name: Text
    points: int = Field(gt=0)
    frequency: str
    is_active: bool = True
    next_due_date: date
    last_completed_at: datetime | None = None
    created_at: datetime | None = None
    external_id: Text | None = None

    @model_validator(mode="after")
    def _check(self) -> ChoreRow:
        if self.frequency not in FREQUENCIES:
            raise ValueError(f"frequency must be one of {', '.join(FREQUENCIES)}")
        return self


class CompletionRow(_Row):
    external_id: Text
    external_chore_id: Text | None = None
    member: Slug | None = None
    chore: Text | None = None
    completed_at: datetime
    points_awarded: int
    note: str | None = None

    @model_validator(mode="after")
    def _check(self) -> CompletionRow:
        by_name = self.member is not None and self.chore is not None
        if (self.external_chore_id is None) == (not by_name):
            raise ValueError("give external_chore_id, or member and chore, but not both")
        return self


class TransactionRow(_Row):
    external_id: Text
    member: Slug
    amount: int
    description: Text
    source: str
    created_at: datetime
    completion_external_id: Text | None = None

    @model_validator(mode="after")
    def _check(self) -> TransactionRow:
        if self.source not in SOURCES:
            raise ValueError(f"source must be one of {', '.join(SOURCES)}")
        return self


def _parse(section: str, model: type[_Row], rows: list[Any] | None, errors: list[str]) -> list:
    parsed = []
    for index, raw in enumerate(rows or []):
        try:
            row = model.model_validate(raw)
            row._index = index
            parsed.append(row)
        except ValidationError as exc:
            for error in exc.errors():
                where = "".join(
                    f".{part}" if isinstance(part, str) else f"[{part}]" for part in error["loc"]
                )
                errors.append(f"{section}[{index}]{where}: {error['msg']}")
    return parsed


def _duplicates(section: str, keys: list[tuple[int, str | None]], errors: list[str]) -> None:
    seen: set[str] = set()
    for index, key in keys:
        if key is None:
            continue
        if key in seen:
            errors.append(f"{section}[{index}]: {key!r} is in the batch more than one time")
        seen.add(key)


def _new_counts() -> dict[str, dict[str, int]]:
    return {
        table: {"created": 0, "skipped": 0}
        for table in ("members", "chores", "completions", "transactions")
    }


async def _find_chore(session: AsyncSession, member_id: int, row: ChoreRow) -> Chore | None:
    if row.external_id is not None:
        found = await session.scalar(select(Chore).where(Chore.import_id == row.external_id))
        if found is not None:
            return found
    query = select(Chore).where(Chore.member_id == member_id, Chore.name == row.name)
    if row.created_at is not None:
        query = query.where(Chore.created_at == _utc(row.created_at))
    return (await session.scalars(query.order_by(Chore.id).limit(1))).first()


async def import_data(
    session: AsyncSession,
    version: int,
    members: list[Any] | None = None,
    chores: list[Any] | None = None,
    completions: list[Any] | None = None,
    transactions: list[Any] | None = None,
) -> dict[str, Any]:
    """Import one batch. See the module docstring and ``docs/import.md``.

    A batch that is not valid raises ``InvalidArgument`` with one line for
    each problem, and writes nothing. The result has the ``created`` and
    ``skipped`` counts of each table, and the balance of each member that
    the batch names.
    """
    if version != FORMAT_VERSION:
        raise InvalidArgument(f"version must be {FORMAT_VERSION}, not {version!r}")

    errors: list[str] = []
    member_rows: list[MemberRow] = _parse("members", MemberRow, members, errors)
    chore_rows: list[ChoreRow] = _parse("chores", ChoreRow, chores, errors)
    completion_rows: list[CompletionRow] = _parse("completions", CompletionRow, completions, errors)
    transaction_rows: list[TransactionRow] = _parse(
        "transactions", TransactionRow, transactions, errors
    )
    _duplicates("members", [(row._index, row.slug) for row in member_rows], errors)
    _duplicates("chores", [(row._index, row.external_id) for row in chore_rows], errors)
    _duplicates("completions", [(row._index, row.external_id) for row in completion_rows], errors)
    _duplicates("transactions", [(row._index, row.external_id) for row in transaction_rows], errors)

    # Each slug that a row names must be in the batch or in the database.
    known_slugs = {row.slug for row in member_rows}
    known_slugs |= set((await session.scalars(select(Member.slug))).all())
    batch_chores = {row.external_id for row in chore_rows if row.external_id is not None}
    batch_completions = {row.external_id for row in completion_rows}
    for section, rows in (
        ("chores", chore_rows),
        ("completions", completion_rows),
        ("transactions", transaction_rows),
    ):
        for row in rows:
            if row.member is not None and row.member not in known_slugs:
                errors.append(
                    f"{section}[{row._index}].member: member {row.member!r} does not exist"
                )
    for row in completion_rows:
        ref = row.external_chore_id
        if ref is not None and ref not in batch_chores:
            if await session.scalar(select(Chore.id).where(Chore.import_id == ref)) is None:
                errors.append(
                    f"completions[{row._index}].external_chore_id: chore {ref!r} does not exist"
                )
    for row in transaction_rows:
        ref = row.completion_external_id
        if ref is not None and ref not in batch_completions:
            if (
                await session.scalar(select(Completion.id).where(Completion.import_id == ref))
                is None
            ):
                errors.append(
                    f"transactions[{row._index}].completion_external_id: "
                    f"completion {ref!r} does not exist"
                )
    if errors:
        raise InvalidArgument("import rejected:\n" + "\n".join(f"- {error}" for error in errors))

    counts = _new_counts()
    try:
        await _write(session, member_rows, chore_rows, completion_rows, transaction_rows, counts)
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    slugs = (
        [row.slug for row in member_rows]
        + [row.member for row in chore_rows]
        + [row.member for row in transaction_rows]
    )
    balances: dict[str, int] = {}
    for slug in dict.fromkeys(slugs):
        member_id = await session.scalar(select(Member.id).where(Member.slug == slug))
        balances[slug] = await balance(session, member_id)
    return {"version": version, "counts": counts, "balances": balances}


async def _write(
    session: AsyncSession,
    member_rows: list[MemberRow],
    chore_rows: list[ChoreRow],
    completion_rows: list[CompletionRow],
    transaction_rows: list[TransactionRow],
    counts: dict[str, dict[str, int]],
) -> None:
    member_ids: dict[str, int] = {}

    async def member_id(slug: str) -> int:
        if slug not in member_ids:
            member_ids[slug] = await session.scalar(select(Member.id).where(Member.slug == slug))
        return member_ids[slug]

    for row in member_rows:
        if await session.scalar(select(Member.id).where(Member.slug == row.slug)) is not None:
            counts["members"]["skipped"] += 1
            continue
        session.add(
            Member(slug=row.slug, name=row.name, is_active=row.is_active, sort_order=row.sort_order)
        )
        await session.flush()
        counts["members"]["created"] += 1

    chore_ids: dict[str, int] = {}
    for row in chore_rows:
        owner = await member_id(row.member)
        chore = await _find_chore(session, owner, row)
        if chore is not None:
            counts["chores"]["skipped"] += 1
        else:
            chore = Chore(
                member_id=owner,
                name=row.name,
                points=row.points,
                frequency=row.frequency,
                is_active=row.is_active,
                next_due_date=row.next_due_date,
                last_completed_at=_utc(row.last_completed_at),
                import_id=row.external_id,
            )
            if row.created_at is not None:
                chore.created_at = _utc(row.created_at)
                chore.updated_at = chore.created_at
            session.add(chore)
            await session.flush()
            counts["chores"]["created"] += 1
        if row.external_id is not None:
            chore_ids[row.external_id] = chore.id

    completion_ids: dict[str, int] = {}
    for row in completion_rows:
        existing = await session.scalar(
            select(Completion.id).where(Completion.import_id == row.external_id)
        )
        if existing is not None:
            completion_ids[row.external_id] = existing
            counts["completions"]["skipped"] += 1
            continue
        chore = await _completion_chore(session, row, chore_ids, member_id)
        if chore is None:
            raise InvalidArgument(
                f"completions[{row._index}]: "
                f"chore {row.chore!r} of member {row.member!r} does not exist"
            )
        completion = Completion(
            chore_id=chore.id,
            member_id=chore.member_id,
            completed_at=_utc(row.completed_at),
            points_awarded=row.points_awarded,
            note=row.note,
            import_id=row.external_id,
        )
        session.add(completion)
        await session.flush()
        completion_ids[row.external_id] = completion.id
        counts["completions"]["created"] += 1

    for row in transaction_rows:
        if (
            await session.scalar(
                select(Transaction.id).where(Transaction.import_id == row.external_id)
            )
            is not None
        ):
            counts["transactions"]["skipped"] += 1
            continue
        reference = None
        if row.completion_external_id is not None:
            reference = completion_ids.get(row.completion_external_id) or await session.scalar(
                select(Completion.id).where(Completion.import_id == row.completion_external_id)
            )
        session.add(
            Transaction(
                member_id=await member_id(row.member),
                amount=row.amount,
                description=row.description,
                source=row.source,
                reference_id=reference,
                actor=IMPORT_ACTOR,
                created_at=_utc(row.created_at),
                import_id=row.external_id,
            )
        )
        counts["transactions"]["created"] += 1
    await session.flush()


async def _completion_chore(
    session: AsyncSession,
    row: CompletionRow,
    chore_ids: dict[str, int],
    member_id: Any,
) -> Chore | None:
    if row.external_chore_id is not None:
        if row.external_chore_id in chore_ids:
            return await session.get(Chore, chore_ids[row.external_chore_id])
        return await session.scalar(select(Chore).where(Chore.import_id == row.external_chore_id))
    owner = await member_id(row.member)
    found = list(
        (
            await session.scalars(
                select(Chore).where(Chore.member_id == owner, Chore.name == row.chore).limit(2)
            )
        ).all()
    )
    if len(found) > 1:
        raise InvalidArgument(
            f"member {row.member!r} has more than one chore {row.chore!r}; use external_chore_id"
        )
    return found[0] if found else None
