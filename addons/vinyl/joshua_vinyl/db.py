"""The SQLite store under the data directory.

One file, ``vinyl.db``. The schema follows the PRD: ``albums``, ``tracks``,
``prices``, ``tags``, ``plays``, plus ``artists`` (the MusicBrainz sort-name
cache, keyed by artist name) and ``meta`` (the last sync). The collection
itself lives on Discogs, so the file is fully reconstructible, and it is
never in the critical path of a backup.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
    discogs_release_id INTEGER PRIMARY KEY,
    instance_id INTEGER,
    artist TEXT NOT NULL,
    artist_sort TEXT NOT NULL,
    artist_sort_source TEXT NOT NULL,
    title TEXT NOT NULL,
    year INTEGER,
    label TEXT,
    catalog_no TEXT,
    format TEXT,
    country TEXT,
    genres TEXT NOT NULL,
    styles TEXT NOT NULL,
    facets TEXT NOT NULL,
    primary_facet TEXT,
    is_compilation INTEGER NOT NULL DEFAULT 0,
    is_soundtrack INTEGER NOT NULL DEFAULT 0,
    thumb_path TEXT,
    art_path TEXT,
    added_at TEXT,
    shelf_section TEXT NOT NULL,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS tracks (
    release_id INTEGER NOT NULL,
    position TEXT NOT NULL,
    title TEXT NOT NULL,
    duration TEXT,
    PRIMARY KEY (release_id, position, title)
);
CREATE TABLE IF NOT EXISTS prices (
    release_id INTEGER PRIMARY KEY,
    lowest_price REAL,
    currency TEXT,
    num_for_sale INTEGER,
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tags (
    release_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    source TEXT NOT NULL,
    count INTEGER,
    PRIMARY KEY (release_id, tag, source)
);
CREATE TABLE IF NOT EXISTS plays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id INTEGER NOT NULL,
    played_at TEXT NOT NULL,
    person TEXT,
    mood TEXT,
    source TEXT
);
CREATE TABLE IF NOT EXISTS artists (
    name TEXT PRIMARY KEY,
    sort_name TEXT NOT NULL,
    type TEXT,
    source TEXT NOT NULL,
    mbid TEXT,
    resolved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

LIST_COLUMNS = ("genres", "styles", "facets")

# A column the collection item does not carry. A sync keeps the value it has
# when the new row has none, so a failed release fetch loses nothing.
KEPT_COLUMNS = ("country", "notes")


def connect(path: Path) -> sqlite3.Connection:
    """Open (and create) the database at ``path`` with the schema applied."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_album(conn: sqlite3.Connection, album: dict[str, Any]) -> None:
    """Insert one album row, or update the columns the collection supplies.

    A collection item does not carry every column. The country comes from
    the release, and the note is written by hand, so a plain replace would
    empty both on every sync and lose them for good when the release fetch
    fails. Those columns keep their value when the new row has none.
    """
    row = dict(album)
    for column in LIST_COLUMNS:
        row[column] = json.dumps(row.get(column) or [])
    row["is_compilation"] = int(bool(row.get("is_compilation")))
    row["is_soundtrack"] = int(bool(row.get("is_soundtrack")))
    columns = list(row)
    placeholders = ", ".join(f":{key}" for key in columns)
    updates = []
    for column in columns:
        if column == "discogs_release_id":
            continue
        if column in KEPT_COLUMNS:
            updates.append(f"{column} = COALESCE(excluded.{column}, albums.{column})")
        else:
            updates.append(f"{column} = excluded.{column}")
    conn.execute(
        f"INSERT INTO albums ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(discogs_release_id) DO UPDATE SET {', '.join(updates)}",
        row,
    )


def delete_albums_not_in(conn: sqlite3.Connection, keep: set[int]) -> int:
    """Remove every album whose release id is not in ``keep``. Return the count removed."""
    rows = conn.execute("SELECT discogs_release_id FROM albums").fetchall()
    gone = [row["discogs_release_id"] for row in rows if row["discogs_release_id"] not in keep]
    for release_id in gone:
        conn.execute("DELETE FROM albums WHERE discogs_release_id = ?", (release_id,))
        conn.execute("DELETE FROM tracks WHERE release_id = ?", (release_id,))
        conn.execute("DELETE FROM prices WHERE release_id = ?", (release_id,))
        conn.execute("DELETE FROM tags WHERE release_id = ?", (release_id,))
    return len(gone)


def _album_from_row(row: sqlite3.Row) -> dict[str, Any]:
    album = dict(row)
    for column in LIST_COLUMNS:
        album[column] = json.loads(album[column] or "[]")
    album["is_compilation"] = bool(album["is_compilation"])
    album["is_soundtrack"] = bool(album["is_soundtrack"])
    return album


def list_albums(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every album, ordered by artist sort-name, then year, then title."""
    rows = conn.execute("SELECT * FROM albums ORDER BY artist_sort, year, title").fetchall()
    return [_album_from_row(row) for row in rows]


def get_album(conn: sqlite3.Connection, release_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM albums WHERE discogs_release_id = ?", (release_id,)
    ).fetchone()
    return _album_from_row(row) if row else None


def list_tracks(conn: sqlite3.Connection, release_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT position, title, duration FROM tracks WHERE release_id = ? ORDER BY rowid",
        (release_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def replace_tracks(conn: sqlite3.Connection, release_id: int, tracks: list[dict[str, Any]]) -> None:
    """Replace the tracklist of one release."""
    conn.execute("DELETE FROM tracks WHERE release_id = ?", (release_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO tracks (release_id, position, title, duration) VALUES (?, ?, ?, ?)",
        [(release_id, t["position"], t["title"], t.get("duration")) for t in tracks],
    )


def set_album_country(conn: sqlite3.Connection, release_id: int, country: str) -> None:
    conn.execute(
        "UPDATE albums SET country = ? WHERE discogs_release_id = ?", (country, release_id)
    )


def put_price(
    conn: sqlite3.Connection,
    release_id: int,
    *,
    lowest_price: float | None,
    currency: str | None,
    num_for_sale: int | None,
    checked_at: str,
) -> None:
    """Record the marketplace summary of one release, and when it was checked."""
    conn.execute(
        "INSERT OR REPLACE INTO prices (release_id, lowest_price, currency, num_for_sale,"
        " checked_at) VALUES (?, ?, ?, ?, ?)",
        (release_id, lowest_price, currency, num_for_sale, checked_at),
    )


def get_price(conn: sqlite3.Connection, release_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM prices WHERE release_id = ?", (release_id,)).fetchone()
    return dict(row) if row else None


def list_tags(conn: sqlite3.Connection, release_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT tag, source, count FROM tags WHERE release_id = ? ORDER BY count DESC, tag",
        (release_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_artist(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM artists WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def put_artist(
    conn: sqlite3.Connection,
    name: str,
    sort_name: str,
    *,
    type_: str | None,
    source: str,
    mbid: str | None,
    resolved_at: str,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO artists (name, sort_name, type, source, mbid, resolved_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (name, sort_name, type_, source, mbid, resolved_at),
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
