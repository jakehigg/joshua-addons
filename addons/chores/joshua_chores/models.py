"""SQLAlchemy ORM models for the chores addon.

``Base`` is the declarative base for every table. ``UTCDateTime`` keeps each
``datetime`` column a timezone-aware UTC value on SQLite and on Postgres.

The four tables are ``members``, ``chores``, ``completions``, and
``transactions``. No table stores a balance. The balance of a member is the
sum of the ``amount`` column of the transactions of that member (see
``service.balance``).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

# The schedule of a chore. ``schedule.py`` calculates the due dates.
FREQUENCIES: tuple[str, ...] = ("daily", "weekly", "monthly", "one_off")

# The origin of a ledger row: a completed chore, a manager award, a manager
# deduction, or a correction.
SOURCES: tuple[str, ...] = ("chore", "one_off", "withdrawal", "adjustment")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


class UTCDateTime(TypeDecorator):
    """A timezone-aware UTC ``datetime``, identical on both dialects.

    Postgres (asyncpg) returns a tz-aware ``datetime`` for
    ``DateTime(timezone=True)``. SQLite keeps the same value as an ISO string,
    and its driver returns a naive ``datetime``. Every value here is UTC, so
    this type stamps ``UTC`` on each naive value it writes or reads. The two
    dialects then give the same result to the rest of this package.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    # Each Python ``datetime`` column is a UTC instant. Map the type once here,
    # so no column has to name ``UTCDateTime()``.
    type_annotation_map = {datetime: UTCDateTime()}


def _now() -> datetime:
    return datetime.now(UTC)


class Member(Base):
    """A person who does chores and has an XP balance."""

    __tablename__ = "members"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    # The kiosk PIN of the member. The addon does not use it yet.
    pin_hash: Mapped[str | None] = mapped_column(Text, default=None)
    is_active: Mapped[bool] = mapped_column(default=True)
    sort_order: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=_now)

    chores: Mapped[list[Chore]] = relationship(back_populates="member")


class Chore(Base):
    """A task of one member, with its XP value and its schedule."""

    __tablename__ = "chores"
    __table_args__ = (
        CheckConstraint(_in_list("frequency", FREQUENCIES), name="ck_chore_frequency"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    name: Mapped[str] = mapped_column(Text)
    points: Mapped[int]
    frequency: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=True)
    next_due_date: Mapped[date]
    last_completed_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)
    # The ``external_id`` of the row in an ``import_data`` file. NULL for a
    # row that the addon wrote itself.
    import_id: Mapped[str | None] = mapped_column(Text, default=None, unique=True)

    member: Mapped[Member] = relationship(back_populates="chores")
    # The database deletes the completions of a deleted chore (ON DELETE
    # CASCADE), so the ORM does not load them first.
    completions: Mapped[list[Completion]] = relationship(
        back_populates="chore", passive_deletes=True
    )


class Completion(Base):
    """One time that a member completed a chore."""

    __tablename__ = "completions"

    id: Mapped[int] = mapped_column(primary_key=True)
    chore_id: Mapped[int] = mapped_column(ForeignKey("chores.id", ondelete="CASCADE"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    completed_at: Mapped[datetime] = mapped_column(default=_now)
    points_awarded: Mapped[int]
    note: Mapped[str | None] = mapped_column(Text, default=None)
    # The ``external_id`` of the row in an ``import_data`` file. NULL for a
    # row that the addon wrote itself.
    import_id: Mapped[str | None] = mapped_column(Text, default=None, unique=True)

    chore: Mapped[Chore] = relationship(back_populates="completions")


class Transaction(Base):
    """One ledger row. ``amount`` is positive for XP in and negative for XP out."""

    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint(_in_list("source", SOURCES), name="ck_transaction_source"),
        Index("ix_transactions_member_id_created_at", "member_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"))
    amount: Mapped[int]
    description: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    # The completion id when ``source`` is ``chore``. Otherwise NULL.
    reference_id: Mapped[int | None] = mapped_column(default=None)
    # The caller that wrote the row, when the caller gives one.
    actor: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    # The ``external_id`` of the row in an ``import_data`` file. NULL for a
    # row that the addon wrote itself.
    import_id: Mapped[str | None] = mapped_column(Text, default=None, unique=True)
