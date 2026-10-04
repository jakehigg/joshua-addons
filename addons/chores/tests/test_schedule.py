"""Due-date arithmetic: next_due and advance_to_current."""

from __future__ import annotations

from datetime import date

import pytest
from joshua_chores.schedule import add_months, advance_to_current, next_due


def test_daily_adds_one_day() -> None:
    assert next_due("daily", date(2026, 12, 31)) == date(2027, 1, 1)


def test_weekly_adds_seven_days() -> None:
    assert next_due("weekly", date(2026, 2, 25)) == date(2026, 3, 4)


def test_monthly_adds_one_calendar_month() -> None:
    assert next_due("monthly", date(2026, 3, 15)) == date(2026, 4, 15)


def test_monthly_crosses_the_year() -> None:
    assert next_due("monthly", date(2026, 12, 10)) == date(2027, 1, 10)


def test_monthly_clamps_jan_31_to_feb_28() -> None:
    assert next_due("monthly", date(2026, 1, 31)) == date(2026, 2, 28)


def test_monthly_clamps_jan_31_to_feb_29_in_a_leap_year() -> None:
    assert next_due("monthly", date(2028, 1, 31)) == date(2028, 2, 29)


def test_monthly_clamps_to_a_30_day_month() -> None:
    assert next_due("monthly", date(2026, 3, 31)) == date(2026, 4, 30)


def test_one_off_returns_the_same_date() -> None:
    assert next_due("one_off", date(2026, 5, 5)) == date(2026, 5, 5)


def test_unknown_frequency_raises() -> None:
    with pytest.raises(ValueError, match="unknown frequency"):
        next_due("hourly", date(2026, 1, 1))
    with pytest.raises(ValueError, match="unknown frequency"):
        advance_to_current(date(2026, 1, 1), "yearly", date(2026, 2, 1))


def test_add_months_by_twelve_and_more() -> None:
    assert add_months(date(2026, 1, 31), 13) == date(2027, 2, 28)
    assert add_months(date(2026, 5, 31), 0) == date(2026, 5, 31)


@pytest.mark.parametrize("frequency", ["daily", "weekly", "monthly", "one_off"])
def test_advance_does_not_change_a_date_on_or_after_today(frequency: str) -> None:
    today = date(2026, 6, 10)
    assert advance_to_current(today, frequency, today) == today
    assert advance_to_current(date(2026, 6, 20), frequency, today) == date(2026, 6, 20)


def test_advance_daily_overdue_is_today() -> None:
    assert advance_to_current(date(2026, 5, 1), "daily", date(2026, 6, 10)) == date(2026, 6, 10)


def test_advance_one_off_overdue_does_not_change() -> None:
    assert advance_to_current(date(2026, 5, 1), "one_off", date(2026, 6, 10)) == date(2026, 5, 1)


def test_advance_weekly_lands_on_today_when_the_week_matches() -> None:
    assert advance_to_current(date(2026, 6, 3), "weekly", date(2026, 6, 10)) == date(2026, 6, 10)


def test_advance_weekly_lands_on_the_next_period() -> None:
    assert advance_to_current(date(2026, 6, 1), "weekly", date(2026, 6, 10)) == date(2026, 6, 15)


def test_advance_monthly_one_period() -> None:
    assert advance_to_current(date(2026, 5, 20), "monthly", date(2026, 6, 10)) == date(2026, 6, 20)


def test_advance_monthly_when_the_day_already_passed_this_month() -> None:
    assert advance_to_current(date(2026, 5, 5), "monthly", date(2026, 6, 10)) == date(2026, 7, 5)


def test_advance_far_overdue_monthly_keeps_the_month_end() -> None:
    assert advance_to_current(date(2025, 1, 31), "monthly", date(2026, 6, 15)) == date(2026, 6, 30)
    assert advance_to_current(date(2025, 1, 31), "monthly", date(2026, 2, 1)) == date(2026, 2, 28)


def test_advance_far_overdue_weekly() -> None:
    result = advance_to_current(date(2024, 1, 1), "weekly", date(2026, 6, 10))
    assert result >= date(2026, 6, 10)
    assert (result - date(2024, 1, 1)).days % 7 == 0
    assert (result - date(2026, 6, 10)).days < 7
