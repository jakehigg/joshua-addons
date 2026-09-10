"""Manual corrections: in the database, written by a tool, applied at once."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import FakeDiscogs, FakeMusicBrainz, sync_into
from joshua_vinyl import db, intake
from joshua_vinyl.bundle import read_index
from joshua_vinyl.config import Overrides, ShelfConfig
from joshua_vinyl.sync import META_OVERRIDES_IMPORTED, migrate_overrides, refile_albums

GOLDBERG = 11059794
NUGGETS = 2025582


@pytest.fixture
def synced_dir(
    data_dir: Path, fake_discogs: FakeDiscogs, fake_musicbrainz: FakeMusicBrainz
) -> Path:
    sync_into(data_dir, fake_discogs, fake_musicbrainz)
    return data_dir


def _correct(data_dir: Path, kind: str, key: str, value: str | None, **kwargs: Any) -> dict:
    conn = db.connect(data_dir / "vinyl.db")
    try:
        return intake.set_correction(
            conn,
            kind=kind,
            key=key,
            value=value,
            config=ShelfConfig(),
            bundle_dir=data_dir / "bundle",
            **kwargs,
        )
    finally:
        conn.close()


def _section_of(data_dir: Path, release_id: int) -> str:
    index = read_index(data_dir / "bundle")
    return next(r["section"] for r in index["records"] if r["id"] == release_id)


def test_a_sort_name_correction_moves_the_record_at_once(synced_dir: Path) -> None:
    before = _section_of(synced_dir, GOLDBERG)
    result = _correct(synced_dir, db.ARTIST_SORT, "Glenn Gould", "Bach, Johann Sebastian")
    assert result["action"] == "set"
    assert _section_of(synced_dir, GOLDBERG) == "B"
    assert before != "B"
    assert any(record["id"] == GOLDBERG for record in result["moved"])


def test_dropping_a_correction_puts_the_record_back(synced_dir: Path) -> None:
    _correct(synced_dir, db.ARTIST_SORT, "Glenn Gould", "Bach, Johann Sebastian")
    result = _correct(synced_dir, db.ARTIST_SORT, "Glenn Gould", None)
    assert result["action"] == "cleared"
    assert _section_of(synced_dir, GOLDBERG) == "G"


def test_dropping_a_correction_that_was_never_set_says_so(synced_dir: Path) -> None:
    assert _correct(synced_dir, db.SECTION, "1", None)["action"] == "not set"


def test_a_section_correction_moves_one_record(synced_dir: Path) -> None:
    result = _correct(synced_dir, db.SECTION, str(NUGGETS), "N", release_ids=[NUGGETS])
    assert _section_of(synced_dir, NUGGETS) == "N"
    assert [record["id"] for record in result["moved"]] == [NUGGETS]


def test_a_genre_correction_changes_the_facet(synced_dir: Path) -> None:
    _correct(synced_dir, db.PRIMARY_FACET, str(NUGGETS), "Folk & World", release_ids=[NUGGETS])
    index = read_index(synced_dir / "bundle")
    record = next(r for r in index["records"] if r["id"] == NUGGETS)
    assert record["facet"] == "Folk & World"


def test_a_correction_survives_the_next_sync(
    synced_dir: Path, fake_discogs: FakeDiscogs, fake_musicbrainz: FakeMusicBrainz
) -> None:
    _correct(synced_dir, db.ARTIST_SORT, "Glenn Gould", "Bach, Johann Sebastian")
    sync_into(synced_dir, fake_discogs, fake_musicbrainz)
    assert _section_of(synced_dir, GOLDBERG) == "B"


def test_the_corrections_are_listed(synced_dir: Path) -> None:
    _correct(synced_dir, db.ARTIST_SORT, "Glenn Gould", "Bach, Johann Sebastian")
    _correct(synced_dir, db.SECTION, str(NUGGETS), "N", release_ids=[NUGGETS])
    conn = db.connect(synced_dir / "vinyl.db")
    try:
        listed = intake.corrections(conn)
    finally:
        conn.close()
    assert listed["sort_names"]["Glenn Gould"] == "Bach, Johann Sebastian"
    assert listed["sections"][str(NUGGETS)] == "N"
    assert listed["genres"] == {}


def test_the_rules_file_corrections_are_imported_once(
    data_dir: Path, fake_discogs: FakeDiscogs, fake_musicbrainz: FakeMusicBrainz
) -> None:
    config = ShelfConfig(overrides=Overrides(artist_sort={"Glenn Gould": "Bach, Johann Sebastian"}))
    sync_into(data_dir, fake_discogs, fake_musicbrainz, config)
    conn = db.connect(data_dir / "vinyl.db")
    try:
        assert db.list_overrides(conn)[db.ARTIST_SORT] == {"Glenn Gould": "Bach, Johann Sebastian"}
        assert db.get_meta(conn, META_OVERRIDES_IMPORTED)
    finally:
        conn.close()
    assert _section_of(data_dir, GOLDBERG) == "B"


def test_a_correction_dropped_by_hand_does_not_come_back(
    data_dir: Path, fake_discogs: FakeDiscogs, fake_musicbrainz: FakeMusicBrainz
) -> None:
    """The reason the import runs once, not on every sync."""
    config = ShelfConfig(overrides=Overrides(artist_sort={"Glenn Gould": "Bach, Johann Sebastian"}))
    sync_into(data_dir, fake_discogs, fake_musicbrainz, config)
    _correct(data_dir, db.ARTIST_SORT, "Glenn Gould", None)
    sync_into(data_dir, fake_discogs, fake_musicbrainz, config)
    assert _section_of(data_dir, GOLDBERG) == "G"


def test_the_import_never_overwrites_what_a_tool_wrote(
    data_dir: Path, fake_discogs: FakeDiscogs, fake_musicbrainz: FakeMusicBrainz
) -> None:
    sync_into(data_dir, fake_discogs, fake_musicbrainz)
    conn = db.connect(data_dir / "vinyl.db")
    try:
        db.set_meta(conn, META_OVERRIDES_IMPORTED, "")
        db.set_override(conn, db.ARTIST_SORT, "Glenn Gould", "Gould, Glenn", "2026-01-01T00:00:00")
        conn.commit()
        migrate_overrides(
            conn,
            ShelfConfig(overrides=Overrides(artist_sort={"Glenn Gould": "Bach, Johann Sebastian"})),
            now=__import__("datetime").datetime.now(__import__("datetime").UTC),
        )
        assert db.list_overrides(conn)[db.ARTIST_SORT]["Glenn Gould"] == "Gould, Glenn"
    finally:
        conn.close()


def test_refiling_writes_only_what_changed(synced_dir: Path) -> None:
    conn = db.connect(synced_dir / "vinyl.db")
    try:
        assert refile_albums(conn, ShelfConfig()) == []
    finally:
        conn.close()
