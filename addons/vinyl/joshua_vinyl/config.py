"""Settings from the environment, and the shelf rules from a JSON file.

Every setting has an environment variable, and ``README.md`` documents each
one. The shelf rules (facets, special sections, manual overrides) are
household taste, so they live in one JSON file that the person who runs the
addon keeps, at ``VINYL_CONFIG``. When the file is absent, the defaults apply.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

DATA_DIR_ENV = "VINYL_DATA_DIR"
DEFAULT_DATA_DIR = "/data"
STATIC_DIR_ENV = "VINYL_STATIC_DIR"
DEFAULT_STATIC_DIR = str(Path(__file__).resolve().parent / "static")
CONFIG_ENV = "VINYL_CONFIG"
TOKEN_ENV = "DISCOGS_TOKEN"
USERNAME_ENV = "DISCOGS_USERNAME"
USER_AGENT_ENV = "VINYL_USER_AGENT"
DEFAULT_USER_AGENT = "joshua-vinyl/0.1 (+https://github.com/jakehigg/joshua-addons)"
SYNC_TIME_ENV = "VINYL_SYNC_TIME"
DEFAULT_SYNC_TIME = "03:00"


class Settings(BaseModel):
    """The process settings, read once from the environment."""

    data_dir: Path
    static_dir: Path
    config_path: Path
    discogs_token: str | None
    discogs_username: str | None
    user_agent: str
    sync_time: str | None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "vinyl.db"

    @property
    def art_dir(self) -> Path:
        return self.data_dir / "art"

    @property
    def bundle_dir(self) -> Path:
        return self.data_dir / "bundle"


def settings_from_env() -> Settings:
    """Read every setting from the environment. Never logs the token."""
    data_dir = Path(os.environ.get(DATA_DIR_ENV) or DEFAULT_DATA_DIR)
    config_path = Path(os.environ.get(CONFIG_ENV) or (data_dir / "config.json"))
    sync_time = os.environ.get(SYNC_TIME_ENV, DEFAULT_SYNC_TIME).strip() or None
    return Settings(
        data_dir=data_dir,
        static_dir=Path(os.environ.get(STATIC_DIR_ENV) or DEFAULT_STATIC_DIR),
        config_path=config_path,
        discogs_token=os.environ.get(TOKEN_ENV) or None,
        discogs_username=os.environ.get(USERNAME_ENV) or None,
        user_agent=os.environ.get(USER_AGENT_ENV) or DEFAULT_USER_AGENT,
        sync_time=sync_time,
    )


class Section(BaseModel):
    """One named shelf divider that sorts after Z, matched by release traits."""

    name: str
    traits: list[str] = Field(min_length=1)


class FacetRules(BaseModel):
    """How Discogs genres and styles become the top-level facet list.

    ``promote`` maps a style to the genre it leaves. A release with that
    style shows under the style, and the genre it left is dropped from that
    release. ``relabel`` renames a facet for display.
    """

    promote: dict[str, str] = Field(default_factory=lambda: {"Country": "Folk, World, & Country"})
    relabel: dict[str, str] = Field(
        default_factory=lambda: {"Folk, World, & Country": "Folk & World"}
    )


class Overrides(BaseModel):
    """Manual corrections. Keys are the artist name, or the release id as a string."""

    artist_sort: dict[str, str] = Field(default_factory=dict)
    primary_facet: dict[str, str] = Field(default_factory=dict)
    section: dict[str, str] = Field(default_factory=dict)


class ShelfConfig(BaseModel):
    """The shelf rules: facets, special sections, and overrides."""

    facets: FacetRules = Field(default_factory=FacetRules)
    sections: list[Section] = Field(
        default_factory=lambda: [
            Section(name="Compilations & Soundtracks", traits=["compilation", "soundtrack"])
        ]
    )
    overrides: Overrides = Field(default_factory=Overrides)

    @field_validator("sections")
    @classmethod
    def _unique_names(cls, sections: list[Section]) -> list[Section]:
        names = [section.name for section in sections]
        if len(names) != len(set(names)):
            raise ValueError("each section needs a different name")
        return sections


def load_shelf_config(path: Path) -> ShelfConfig:
    """Read the shelf rules from ``path``. A missing file means the defaults."""
    if not path.is_file():
        return ShelfConfig()
    return ShelfConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))
