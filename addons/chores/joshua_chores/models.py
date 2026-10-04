"""SQLAlchemy ORM models for the chores addon.

``Base`` is the declarative base for every table. ``UTCDateTime`` keeps each
``datetime`` column a timezone-aware UTC value on SQLite and on Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator


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
