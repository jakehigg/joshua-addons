"""The bundle: the index entry shape, the detail entry, and the atomic write."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from joshua_vinyl import bundle, db
from joshua_vinyl.config import ShelfConfig


def _album(release_id: int, **overrides):
    album = {
        "discogs_release_id": release_id,
        "instance_id": 5,
        "artist": "The Beatles",
        "artist_sort": "Beatles, The",
        "artist_sort_source": "musicbrainz",
        "title": "Abbey Road",
        "year": None,
        "label": "Apple Records",
        "catalog_no": None,
        "format": "Vinyl, LP",
        "country": "UK",
        "genres": ["Rock", "Pop"],
        "styles": [],
        "facets": ["Rock", "Pop"],
        "primary_facet": "Rock",
        "is_compilation": False,
        "is_soundtrack": False,
        "thumb_path": "art/1-thumb.jpg",
        "art_path": None,
        "added_at": "2026-01-01T00:00:00-07:00",
        "shelf_section": "B",
        "notes": None,
    }
    album.update(overrides)
    return album


def test_index_record_omits_missing_values_instead_of_rendering_zero() -> None:
    record = bundle.index_record(_album(1))
    assert record["id"] == 1
    assert "year" not in record
    assert "catalog_no" not in record
    assert "styles" not in record
    assert "cover" not in record
    assert record["thumb"] == "art/1-thumb.jpg"
    assert record["decade"] is None
    assert record["facet"] == "Rock"
    assert record["section"] == "B"


def test_index_record_derives_the_decade() -> None:
    assert bundle.index_record(_album(1, year=1969))["decade"] == 1960
    assert bundle.index_record(_album(1, year=2015))["decade"] == 2010


def test_detail_record_adds_tracks_price_and_tags_only_when_present(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "vinyl.db")
    album = _album(1, year=1969, country="UK")
    db.upsert_album(conn, album)
    bare = bundle.detail_record(conn, album)
    assert bare["country"] == "UK"
    assert bare["compilation"] is False
    assert "tracks" not in bare
    assert "price" not in bare
    assert "tags" not in bare

    conn.execute("INSERT INTO tracks VALUES (1, 'A1', 'Come Together', '4:20')")
    conn.execute("INSERT INTO prices VALUES (1, 30.0, 'USD', 2, '2026-01-02T00:00:00')")
    conn.execute("INSERT INTO tags VALUES (1, 'classic rock', 'lastfm', 8)")
    full = bundle.detail_record(conn, album)
    assert full["tracks"] == [{"position": "A1", "title": "Come Together", "duration": "4:20"}]
    assert full["price"] == {
        "lowest": 30.0,
        "currency": "USD",
        "for_sale": 2,
        "checked_at": "2026-01-02T00:00:00",
    }
    assert full["tags"][0]["tag"] == "classic rock"


def test_a_price_row_with_no_price_is_absent(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "vinyl.db")
    album = _album(1)
    db.upsert_album(conn, album)
    conn.execute("INSERT INTO prices VALUES (1, NULL, NULL, 0, '2026-01-02T00:00:00')")
    assert "price" not in bundle.detail_record(conn, album)


def test_write_bundle_writes_the_index_and_one_detail_per_album(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "vinyl.db")
    db.upsert_album(conn, _album(1, year=1969))
    db.upsert_album(conn, _album(2, artist_sort="Dylan, Bob", shelf_section="D", year=1965))
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "detail").mkdir(parents=True)
    stale = bundle_dir / "detail" / "999.json"
    stale.write_text("{}")

    when = datetime(2026, 9, 8, 3, 0, tzinfo=UTC)
    index = bundle.write_bundle(conn, bundle_dir, ShelfConfig(), now=when)

    assert index["count"] == 2
    assert index["generated_at"] == "2026-09-08T03:00:00+00:00"
    assert index["sections"] == ["B", "D"]
    assert index["section_order"][-1] == "Compilations & Soundtracks"
    assert [(f["name"], f["count"]) for f in index["facets"]] == [("Pop", 2), ("Rock", 2)]
    assert json.loads((bundle_dir / "index.json").read_text(encoding="utf-8")) == index
    assert (bundle_dir / "detail" / "1.json").is_file()
    assert (bundle_dir / "detail" / "2.json").is_file()
    assert not stale.exists(), "a detail file for a release that left is removed"
    assert not list(bundle_dir.rglob("*.tmp")), "no temporary file is left behind"
    assert bundle.read_index(bundle_dir) == index


def test_read_index_is_none_before_the_first_sync(tmp_path: Path) -> None:
    assert bundle.read_index(tmp_path / "nowhere") is None
