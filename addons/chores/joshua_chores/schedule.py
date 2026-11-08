"""Due-date arithmetic for chores.

Each function here is pure. It does not read the clock and does not touch
the database. The caller gives the current date.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from .models import FREQUENCIES


def _check(frequency: str) -> None:
    if frequency not in FREQUENCIES:
        raise ValueError(f"unknown frequency {frequency!r}; use one of {', '.join(FREQUENCIES)}")


def add_months(value: date, months: int) -> date:
    """Return ``value`` plus ``months`` calendar months.

    When the target month has fewer days, the day is the last day of that
    month. For example, January 31 plus one month is February 28, or
    February 29 in a leap year.
    """
    index = value.month - 1 + months
    year = value.year + index // 12
    month = index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def next_due(frequency: str, from_date: date) -> date:
    """Return the due date that follows ``from_date`` for ``frequency``.

    ``daily`` adds one day, ``weekly`` adds seven days, and ``monthly`` adds
    one calendar month (see ``add_months``). ``one_off`` returns
    ``from_date``, because a one-off chore does not repeat.
    """
    _check(frequency)
    if frequency == "daily":
        return from_date + timedelta(days=1)
    if frequency == "weekly":
        return from_date + timedelta(days=7)
    if frequency == "monthly":
        return add_months(from_date, 1)
    return from_date


def advance_to_current(due: date, frequency: str, today: date) -> date:
    """Return the due date of the current period for an overdue chore.

    A due date on or after ``today`` does not change. A ``one_off`` due date
    does not change. An overdue ``daily`` chore is due ``today``. An overdue
    ``weekly`` or ``monthly`` chore moves forward in full periods from
    ``due`` to the first date on or after ``today``. A monthly step counts
    from the original ``due``, so a chore due on the 31st stays on the last
    day of each short month and does not drift to an earlier day.
    """
    _check(frequency)
    if due >= today or frequency == "one_off":
        return due
    if frequency == "daily":
        return today
    if frequency == "weekly":
        weeks = -(-(today - due).days // 7)
        return due + timedelta(days=7 * weeks)
    months = (today.year - due.year) * 12 + (today.month - due.month)
    candidate = add_months(due, months)
    if candidate < today:
        candidate = add_months(due, months + 1)
    return candidate
