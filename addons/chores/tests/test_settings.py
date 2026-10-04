"""Settings from the environment, read at call time."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest
from conftest import mcp_session
from joshua_chores import settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch) -> None:
    names = ("XP_PER_DOLLAR", "COOLDOWN_SECONDS", "MANAGER_LABEL", "CHORES_TZ", "MANAGER_PIN")
    for name in names:
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


def test_chores_tz_defaults_to_utc() -> None:
    assert settings.chores_tz() == ZoneInfo("UTC")


def test_chores_tz_reads_an_iana_name(monkeypatch) -> None:
    monkeypatch.setenv("CHORES_TZ", " America/New_York ")
    assert settings.chores_tz() == ZoneInfo("America/New_York")


@pytest.mark.parametrize("raw", ["Mars/Base", "../etc/passwd", "not a zone"])
def test_chores_tz_refuses_a_bad_name(monkeypatch, raw: str) -> None:
    monkeypatch.setenv("CHORES_TZ", raw)
    with pytest.raises(ValueError, match="CHORES_TZ"):
        settings.chores_tz()


def test_load_checks_every_setting(monkeypatch) -> None:
    loaded = settings.load()
    assert loaded == settings.Settings(100, 60, "Parent", ZoneInfo("UTC"))
    monkeypatch.setenv("XP_PER_DOLLAR", "-3")
    with pytest.raises(ValueError, match="XP_PER_DOLLAR"):
        settings.load()


def test_manager_pin_is_empty_by_default_and_stripped(monkeypatch) -> None:
    assert settings.manager_pin() == ""
    monkeypatch.setenv("MANAGER_PIN", " 4321 ")
    assert settings.load().manager_pin == "4321"


def test_settings_repr_does_not_show_the_pin(monkeypatch) -> None:
    monkeypatch.setenv("MANAGER_PIN", "secret-pin-value")
    assert "secret-pin-value" not in repr(settings.load())


def _messages(exc: BaseException) -> list[str]:
    if isinstance(exc, BaseExceptionGroup):
        return [m for inner in exc.exceptions for m in _messages(inner)]
    return [str(exc)]


@pytest.mark.parametrize(("name", "raw"), [("CHORES_TZ", "Mars/Base"), ("COOLDOWN_SECONDS", "x")])
async def test_a_bad_setting_stops_the_startup(app_factory, monkeypatch, name, raw) -> None:
    monkeypatch.setenv(name, raw)
    app = app_factory(None)
    with pytest.raises(BaseException) as caught:
        async with mcp_session(app):
            pass
    assert any(name in message for message in _messages(caught.value))
