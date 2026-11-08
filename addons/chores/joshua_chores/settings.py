"""Display and behavior settings from environment variables.

Each function reads its variable when it is called, not at import time, so a
caller (a test, for example) can set the variable first. An empty variable
gives the default. A value that is not valid raises ``ValueError`` with the
variable name. The server calls ``load`` in its lifespan, so a bad value stops
the startup.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

XP_PER_DOLLAR_ENV = "XP_PER_DOLLAR"
COOLDOWN_SECONDS_ENV = "COOLDOWN_SECONDS"
MANAGER_LABEL_ENV = "MANAGER_LABEL"
CHORES_TZ_ENV = "CHORES_TZ"
MANAGER_PIN_ENV = "MANAGER_PIN"

DEFAULT_XP_PER_DOLLAR = 100
DEFAULT_COOLDOWN_SECONDS = 60
DEFAULT_MANAGER_LABEL = "Parent"
DEFAULT_CHORES_TZ = "UTC"


def _non_negative_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, not {raw!r}") from None
    if value < 0:
        raise ValueError(f"{name} must be 0 or more, not {value}")
    return value


def xp_per_dollar() -> int:
    """Return the XP that equals one dollar. ``0`` means: show no dollar value."""
    return _non_negative_int(XP_PER_DOLLAR_ENV, DEFAULT_XP_PER_DOLLAR)


def cooldown_seconds() -> int:
    """Return the minimum number of seconds between two completions of one chore."""
    return _non_negative_int(COOLDOWN_SECONDS_ENV, DEFAULT_COOLDOWN_SECONDS)


def manager_label() -> str:
    """Return the word that the UI shows for a manager."""
    return os.environ.get(MANAGER_LABEL_ENV, "").strip() or DEFAULT_MANAGER_LABEL


def chores_tz() -> ZoneInfo:
    """Return the time zone of the household, from an IANA name such as ``Europe/Paris``.

    The addon calculates "today" in this zone: the due dates, and the date
    from which a completed chore moves to its next due date.
    """
    raw = os.environ.get(CHORES_TZ_ENV, "").strip() or DEFAULT_CHORES_TZ
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"{CHORES_TZ_ENV} must be an IANA time zone name, not {raw!r}") from None


def manager_pin() -> str:
    """Return the PIN of the manager routes under ``/api``.

    An empty string means: no PIN is set, and the addon refuses each manager
    route. Never log this value.
    """
    return os.environ.get(MANAGER_PIN_ENV, "").strip()


@dataclass(frozen=True)
class Settings:
    """The settings, read and checked one time."""

    xp_per_dollar: int
    cooldown_seconds: int
    manager_label: str
    tz: ZoneInfo
    # ``repr=False`` keeps the PIN out of a log line or a traceback that
    # shows the settings.
    manager_pin: str = field(default="", repr=False)


def load() -> Settings:
    """Read and check each setting. A bad value raises ``ValueError``."""
    return Settings(
        xp_per_dollar=xp_per_dollar(),
        cooldown_seconds=cooldown_seconds(),
        manager_label=manager_label(),
        tz=chores_tz(),
        manager_pin=manager_pin(),
    )


def format_dollars(points: int, per_dollar: int) -> str | None:
    """Return ``points`` as a dollar string, for example ``"$1.50"`` or ``"-$0.25"``.

    Returns ``None`` when ``per_dollar`` is 0, because 0 means: show no
    dollar value. The value rounds half up to the nearest cent.
    """
    if per_dollar == 0:
        return None
    if per_dollar < 0:
        raise ValueError(f"per_dollar must be 0 or more, not {per_dollar}")
    dollars = (Decimal(abs(points)) / Decimal(per_dollar)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    sign = "-" if points < 0 and dollars else ""
    return f"{sign}${dollars}"
