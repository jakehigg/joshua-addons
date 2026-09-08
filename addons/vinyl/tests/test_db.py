"""The SQLite store: schema, the album round trip, the artist cache, and the meta table."""

from __future__ import annotations

from pathlib import Path

from joshua_vinyl import db


def _album(release_id: int, **overrides):
    album = {
        "discogs_release_id": release_id,
        "instance_id": 1,
        "artist": "Bob Dylan",
        "artist_sort": "Dylan, Bob",
        "artist_sort_source": "musicbrainz",
        "title": "Highway 61 Revisited",
        "year": 1965,
        "label": "Columbia",
        "catalog_no": "CL 2389",
        "format": "Vinyl, LP, Album, Mono",
        "country": "US",
        "genres": ["Rock"],
        "styles": ["Folk Rock", "Blues Rock"],
        "facets": ["Rock"],
        "primary_facet": "Rock",
        "is_compilation": False,
        "is_soundtrack": False,
        "thumb_path": None,
        "art_path": None,
        "added_at": "2026-01-01T00:00:00-07:00",
        "shelf_section": "D",
        "notes": None,
    }
    album.update(overrides)
    return album


def test_connect_creates_every_table_and_the_parent_directory(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "nested" / "vinyl.db")
    names = {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"albums", "tracks", "prices", "tags", "plays", "artists", "meta"} <= names
    conn.close()


def test_album_round_trip_keeps_lists_and_booleans(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "vinyl.db")
    db.upsert_album(conn, _album(1, is_soundtrack=True))
    album = db.get_album(conn, 1)
    assert album is not None
    assert album["styles"] == ["Folk Rock", "Blues Rock"]
    assert album["is_soundtrack"] is True
    assert album["is_compilation"] is False
    assert db.get_album(conn, 999) is None


def test_upsert_replaces_the_same_release(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "vinyl.db")
    db.upsert_album(conn, _album(1))
    db.upsert_album(conn, _album(1, title="Highway 61 Revisited (Mono)"))
    albums = db.list_albums(conn)
    assert len(albums) == 1
    assert albums[0]["title"] == "Highway 61 Revisited (Mono)"


def test_list_albums_orders_by_sort_name_then_year(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "vinyl.db")
    db.upsert_album(conn, _album(1, artist_sort="Dylan, Bob", year=1975))
    db.upsert_album(conn, _album(2, artist_sort="Beatles, The", year=1969))
    db.upsert_album(conn, _album(3, artist_sort="Dylan, Bob", year=1965))
    assert [a["discogs_release_id"] for a in db.list_albums(conn)] == [2, 3, 1]


def test_delete_albums_not_in_removes_the_rows_and_their_children(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "vinyl.db")
    db.upsert_album(conn, _album(1))
    db.upsert_album(conn, _album(2))
    conn.execute("INSERT INTO tracks VALUES (2, 'A1', 'Song', '3:00')")
    conn.execute("INSERT INTO prices VALUES (2, 10.0, 'USD', 3, '2026-01-01')")
    conn.execute("INSERT INTO tags VALUES (2, 'folk', 'lastfm', 5)")
    assert db.delete_albums_not_in(conn, {1}) == 1
    assert [a["discogs_release_id"] for a in db.list_albums(conn)] == [1]
    assert db.list_tracks(conn, 2) == []
    assert db.get_price(conn, 2) is None
    assert db.list_tags(conn, 2) == []


def test_tracks_prices_and_tags_read_back(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "vinyl.db")
    conn.execute("INSERT INTO tracks VALUES (1, 'A1', 'Like A Rolling Stone', '6:13')")
    conn.execute("INSERT INTO tracks VALUES (1, 'A2', 'Tombstone Blues', '5:58')")
    conn.execute("INSERT INTO prices VALUES (1, 25.5, 'USD', 4, '2026-01-01T00:00:00')")
    conn.execute("INSERT INTO tags VALUES (1, 'folk rock', 'lastfm', 5)")
    conn.execute("INSERT INTO tags VALUES (1, '60s', 'lastfm', 9)")
    assert [t["position"] for t in db.list_tracks(conn, 1)] == ["A1", "A2"]
    assert db.get_price(conn, 1)["lowest_price"] == 25.5
    assert [t["tag"] for t in db.list_tags(conn, 1)] == ["60s", "folk rock"]


def test_artist_cache_round_trip(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "vinyl.db")
    assert db.get_artist(conn, "Bob Dylan") is None
    db.put_artist(
        conn,
        "Bob Dylan",
        "Dylan, Bob",
        type_="Person",
        source="musicbrainz",
        mbid="abc",
        resolved_at="2026-01-01T00:00:00+00:00",
    )
    cached = db.get_artist(conn, "Bob Dylan")
    assert cached is not None
    assert cached["sort_name"] == "Dylan, Bob"
    assert cached["type"] == "Person"


def test_meta_round_trip(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "vinyl.db")
    assert db.get_meta(conn, "last_sync") is None
    db.set_meta(conn, "last_sync", "2026-01-01")
    db.set_meta(conn, "last_sync", "2026-01-02")
    assert db.get_meta(conn, "last_sync") == "2026-01-02"
