"""SQLAlchemy models for the tasks addon.

Empty for now: the project, group, and task models land here.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for every model in the addon."""
