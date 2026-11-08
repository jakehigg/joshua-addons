"""SQLAlchemy models for the tasks addon: projects, groups, and tasks.

Ported from the v3 tasks app (``tasks/backend/app/models.py``). The columns
and their meaning are the same. Two storage changes make the schema run
unchanged on SQLite and Postgres:

* Primary and foreign keys are UUIDs kept as 36-character strings
  (``GUID``), so an id reads back as the same UUID string on both dialects.
* Timestamps use ``UTCDateTime`` and are set from Python, so a value on
  SQLite is timezone-aware UTC with microseconds, as on Postgres.

``ON DELETE CASCADE`` (project to groups and tasks) and ``ON DELETE SET
NULL`` (group to tasks) rely on the foreign-key pragma that ``database.py``
turns on for SQLite.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """A timezone-aware UTC ``datetime``, identical on both dialects.

    Postgres (asyncpg) returns a tz-aware ``datetime`` for
    ``DateTime(timezone=True)``. SQLite stores the same value as an ISO
    string but its driver hands back a naive ``datetime``, so every value
    here is UTC and every value that comes back naive is stamped ``UTC``.
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


class GUID(TypeDecorator):
    """A ``uuid.UUID`` stored as a 36-character string on every dialect.

    Accepts a ``UUID`` or its string form on the way in and always hands a
    ``UUID`` back, so an id round-trips as the same string.
    """

    impl = String(36)
    cache_ok = True

    def process_bind_param(self, value: uuid.UUID | str | None, dialect: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, uuid.UUID):
            value = uuid.UUID(str(value))
        return str(value)

    def process_result_value(self, value: str | None, dialect: object) -> uuid.UUID | None:
        return None if value is None else uuid.UUID(value)


class Base(DeclarativeBase):
    # Every naive Python ``datetime`` column is really a UTC instant, and
    # every ``uuid.UUID`` column is a ``GUID``; map both once here.
    type_annotation_map = {datetime: UTCDateTime(), uuid.UUID: GUID()}


def _now() -> datetime:
    return datetime.now(UTC)


# A project's lifecycle. ``done`` is reached through ``complete_project``,
# which refuses while a task is open; ``closed`` ends a project with open
# tasks; ``paused`` keeps a project out of the next-steps list.
PROJECT_STATUSES = ("active", "paused", "done", "closed")
FINISHED_STATUSES = ("done", "closed")

# T-shirt sizes for a task, smallest first.
TASK_SIZES = ("XS", "S", "M", "L", "XL")

# The one row of ``TaskNumber``, the task-number sequence.
TASK_NUMBER_ID = 1


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    # The outcome the tasks work toward. NULL = a plain list that never
    # completes and gets no health signal.
    goal: Mapped[str | None] = mapped_column(Text, default=None)
    target_date: Mapped[date | None] = mapped_column(Date, default=None)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    completed_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, default=_now, server_default=func.now()
    )
    archived_at: Mapped[datetime | None] = mapped_column(default=None)

    tasks: Mapped[list[Task]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    groups: Mapped[list[Group]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )


class Group(Base):
    """A single-level grouping of tasks within a project (no nesting).

    Groups order among themselves via ``position``; a task's membership is
    ``tasks.group_id`` (nullable: ungrouped tasks are the project root list).
    Deleting a group ungroups its tasks, never deletes them.
    """

    __tablename__ = "groups"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[float] = mapped_column(Float, nullable=False)
    # A group is a phase of the project's roadmap; the date is its milestone.
    target_date: Mapped[date | None] = mapped_column(Date, default=None)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, default=_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, default=_now, onupdate=_now, server_default=func.now()
    )

    project: Mapped[Project] = relationship(back_populates="groups")
    tasks: Mapped[list[Task]] = relationship(back_populates="group", passive_deletes=True)


class TaskNumber(Base):
    """The sequence behind ``Task.number``: one row that holds the last number given.

    One sequence for the whole addon, in one row with ``id =
    TASK_NUMBER_ID``. ``service.create_task`` raises ``last_number`` and
    takes the result, so a number is given once and never again, not even
    after the task is deleted.
    """

    __tablename__ = "task_numbers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    last_number: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_project_position", "project_id", "position"),
        Index("ix_tasks_number", "number", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # The short number a person says out loud ("task 24"). It comes from
    # ``TaskNumber`` at creation and never changes.
    number: Mapped[int] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # Optional single-level grouping; NULL = ungrouped (project root list).
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"), nullable=True, index=True, default=None
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    done: Mapped[bool] = mapped_column(nullable=False, default=False)
    done_at: Mapped[datetime | None] = mapped_column(default=None)
    # One of TASK_SIZES, or NULL when not sized.
    size: Mapped[str | None] = mapped_column(String(4), default=None)
    due_date: Mapped[date | None] = mapped_column(Date, default=None)
    # A person's name as given; the addon keeps no roster.
    assignee: Mapped[str | None] = mapped_column(Text, default=None)
    # Order within a container (the root list or one group); smaller = higher.
    # A reorder rewrites its container's positions as (index+1)*1024.
    position: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, default=_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, default=_now, onupdate=_now, server_default=func.now()
    )

    project: Mapped[Project] = relationship(back_populates="tasks")
    group: Mapped[Group | None] = relationship(back_populates="tasks")
