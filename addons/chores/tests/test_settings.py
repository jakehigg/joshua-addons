"""Settings from the environment, read at call time."""

from __future__ import annotations

import pytest
from joshua_chores import settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch) -> None:
    for name in ("XP_PER_DOLLAR", "COOLDOWN_SECONDS", "MANAGER_LABEL"):
        monkeypatch.delenv(name, raising=False)


def test_defaults() -> None:
    assert settings.xp_per_dollar() == 100
    assert settings.cooldown_seconds() == 60
    assert settings.manager_label() == "Parent"


def test_values_are_read_at_call_time(monkeypatch) -> None:
    monkeypatch.setenv("XP_PER_DOLLAR", "50")
    monkeypatch.setenv("COOLDOWN_SECONDS", "0")
    monkeypatch.setenv("MANAGER_LABEL", "  Coach ")
    assert settings.xp_per_dollar() == 50
    assert settings.cooldown_seconds() == 0
    assert settings.manager_label() == "Coach"


def test_empty_values_give_the_defaults(monkeypatch) -> None:
    monkeypatch.setenv("XP_PER_DOLLAR", "")
    monkeypatch.setenv("MANAGER_LABEL", "   ")
    assert settings.xp_per_dollar() == 100
    assert settings.manager_label() == "Parent"


@pytest.mark.parametrize("raw", ["ten", "1.5", "-1"])
def test_bad_values_raise_with_the_variable_name(monkeypatch, raw: str) -> None:
    monkeypatch.setenv("COOLDOWN_SECONDS", raw)
    with pytest.raises(ValueError, match="COOLDOWN_SECONDS"):
        settings.cooldown_seconds()


@pytest.mark.parametrize(
    ("points", "per_dollar", "expected"),
    [
        (150, 100, "$1.50"),
        (0, 100, "$0.00"),
        (-25, 100, "-$0.25"),
        (1, 3, "$0.33"),
        (1, 200, "$0.01"),
        (-1, 1000, "$0.00"),
        (7, 1, "$7.00"),
    ],
)
def test_format_dollars(points: int, per_dollar: int, expected: str) -> None:
    assert settings.format_dollars(points, per_dollar) == expected


def test_format_dollars_hides_at_zero() -> None:
    assert settings.format_dollars(500, 0) is None


def test_format_dollars_refuses_a_negative_rate() -> None:
    with pytest.raises(ValueError):
        settings.format_dollars(5, -1)
