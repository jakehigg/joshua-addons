"""Settings from the environment, and the shelf rules from a file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from joshua_vinyl import config


def test_defaults_when_nothing_is_set(monkeypatch) -> None:
    for name in (
        config.DATA_DIR_ENV,
        config.STATIC_DIR_ENV,
        config.CONFIG_ENV,
        config.TOKEN_ENV,
        config.USERNAME_ENV,
        config.USER_AGENT_ENV,
        config.SYNC_TIME_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    settings = config.settings_from_env()
    assert settings.data_dir == Path(config.DEFAULT_DATA_DIR)
    assert settings.db_path == Path(config.DEFAULT_DATA_DIR) / "vinyl.db"
    assert settings.art_dir == Path(config.DEFAULT_DATA_DIR) / "art"
    assert settings.bundle_dir == Path(config.DEFAULT_DATA_DIR) / "bundle"
    assert settings.config_path == Path(config.DEFAULT_DATA_DIR) / "config.json"
    assert settings.static_dir.name == "static"
    assert settings.discogs_token is None
    assert settings.discogs_username is None
    assert settings.user_agent == config.DEFAULT_USER_AGENT
    assert settings.sync_time == config.DEFAULT_SYNC_TIME
    assert settings.currency == "USD"


def test_currency_is_upper_cased(monkeypatch) -> None:
    monkeypatch.setenv(config.CURRENCY_ENV, " gbp ")
    assert config.settings_from_env().currency == "GBP"


def test_every_setting_reads_its_variable(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(config.STATIC_DIR_ENV, str(tmp_path / "ui"))
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "rules.json"))
    monkeypatch.setenv(config.TOKEN_ENV, "tok")
    monkeypatch.setenv(config.USERNAME_ENV, "example-user")
    monkeypatch.setenv(config.USER_AGENT_ENV, "me/1.0")
    monkeypatch.setenv(config.SYNC_TIME_ENV, "04:30")
    settings = config.settings_from_env()
    assert settings.data_dir == tmp_path
    assert settings.static_dir == tmp_path / "ui"
    assert settings.config_path == tmp_path / "rules.json"
    assert settings.discogs_token == "tok"
    assert settings.discogs_username == "example-user"
    assert settings.user_agent == "me/1.0"
    assert settings.sync_time == "04:30"


def test_an_empty_sync_time_turns_the_schedule_off(monkeypatch) -> None:
    monkeypatch.setenv(config.SYNC_TIME_ENV, "  ")
    assert config.settings_from_env().sync_time is None


def test_shelf_config_defaults() -> None:
    rules = config.ShelfConfig()
    assert rules.facets.promote == {"Country": "Folk, World, & Country"}
    assert rules.facets.relabel == {"Folk, World, & Country": "Folk & World"}
    assert [s.name for s in rules.sections] == ["Compilations & Soundtracks"]
    assert rules.sections[0].traits == ["various", "soundtrack"]
    assert rules.overrides.artist_sort == {}


def test_load_shelf_config_from_a_missing_file_is_the_defaults(tmp_path: Path) -> None:
    assert config.load_shelf_config(tmp_path / "none.json") == config.ShelfConfig()


def test_load_shelf_config_from_a_file(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "facets": {"promote": {}, "relabel": {"Stage & Screen": "Film"}},
                "sections": [{"name": "Soundtracks", "traits": ["soundtrack"]}],
                "overrides": {"artist_sort": {"Bob Dylan": "Zimmerman, Robert"}},
            }
        ),
        encoding="utf-8",
    )
    rules = config.load_shelf_config(path)
    assert rules.facets.promote == {}
    assert rules.facets.relabel == {"Stage & Screen": "Film"}
    assert rules.sections[0].name == "Soundtracks"
    assert rules.overrides.artist_sort["Bob Dylan"] == "Zimmerman, Robert"


def test_two_sections_with_one_name_are_refused() -> None:
    with pytest.raises(ValueError, match="different name"):
        config.ShelfConfig(
            sections=[
                config.Section(name="Other", traits=["compilation"]),
                config.Section(name="Other", traits=["soundtrack"]),
            ]
        )


def test_a_section_needs_at_least_one_trait() -> None:
    with pytest.raises(ValueError):
        config.Section(name="Empty", traits=[])
