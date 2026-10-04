"""Display and behavior settings from environment variables.

Each function reads its variable when it is called, not at import time, so a
caller (a test, for example) can set the variable first. An empty variable
gives the default. A value that is not a non-negative integer raises
``ValueError`` with the variable name.
"""

from __future__ import annotations

import os
from decimal import ROUND_HALF_UP, Decimal

XP_PER_DOLLAR_ENV = "XP_PER_DOLLAR"
COOLDOWN_SECONDS_ENV = "COOLDOWN_SECONDS"
MANAGER_LABEL_ENV = "MANAGER_LABEL"

DEFAULT_XP_PER_DOLLAR = 100
DEFAULT_COOLDOWN_SECONDS = 60
DEFAULT_MANAGER_LABEL = "Parent"


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
