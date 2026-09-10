"""The static bundle the browse interface reads.

One ``index.json`` for the whole collection, plus one ``detail/<id>.json``
for each album. The sync writes both after every run, so the interface never
queries the database and nothing runs while nobody is browsing. Each file is
written to a temporary name and then moved, so a reader never sees a
half-written file.

A record that is out with somebody carries ``lent``, so the page can say who
has it and the suggestion tool can leave it alone.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from joshua_vinyl import db
from joshua_vinyl.config import ShelfConfig
from joshua_vinyl.facets import facet_counts
from joshua_vinyl.shelf import section_order

INDEX_FIELDS = (
    "title",
    "artist",
    "artist_sort",
    "year",
    "label",
    "catalog_no",
    "format",
    "genres",
    "styles",
    "facets",
    "added_at",
)


def _decade(year: int | None) -> int | None:
    return (year // 10) * 10 if year else None


def index_record(album: dict[str, Any]) -> dict[str, Any]:
    """The index entry for one album. A missing value is absent, never zero."""
    record: dict[str, Any] = {"id": album["discogs_release_id"]}
    for field in INDEX_FIELDS:
        value = album.get(field)
        if value not in (None, "", []):
            record[field] = value
    if album.get("master_id"):
        record["master"] = album["master_id"]
    record["decade"] = _decade(album.get("year"))
    record["facet"] = album.get("primary_facet")
    record["section"] = album["shelf_section"]
    if album.get("thumb_path"):
        record["thumb"] = album["thumb_path"]
    if album.get("art_path"):
        record["cover"] = album["art_path"]
    return record


def detail_record(conn: sqlite3.Connection, album: dict[str, Any]) -> dict[str, Any]:
    """The detail entry: the index entry plus country, tracks, price, and tags."""
    release_id = album["discogs_release_id"]
    record = index_record(album)
    for field in ("country", "notes", "instance_id"):
        if album.get(field):
            record[field] = album[field]
    record["compilation"] = album["is_compilation"]
    record["soundtrack"] = album["is_soundtrack"]
    tracks = db.list_tracks(conn, release_id)
    if tracks:
        record["tracks"] = tracks
    price = db.get_price(conn, release_id)
    if price and price.get("lowest_price") is not None:
        record["price"] = {
            "lowest": price["lowest_price"],
            "currency": price.get("currency"),
            "for_sale": price.get("num_for_sale"),
            "checked_at": price["checked_at"],
        }
    loan = db.get_loan(conn, release_id)
    if loan:
        record["lent"] = {
            "to": loan["person"],
            "since": loan["since"],
            **({"note": loan["note"]} if loan["note"] else {}),
        }
    tags = db.list_tags(conn, release_id)
    if tags:
        record["tags"] = tags
    return record


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def write_bundle(
    conn: sqlite3.Connection,
    bundle_dir: Path,
    config: ShelfConfig,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Write ``index.json`` and every ``detail/<id>.json``. Return the index."""
    albums = db.list_albums(conn)
    loans = db.list_loans(conn)
    records = []
    for album in albums:
        record = index_record(album)
        loan = loans.get(record["id"])
        if loan:
            record["lent"] = loan
        records.append(record)
    order = section_order(config.sections)
    present = {record["section"] for record in records}
    index = {
        "generated_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "count": len(records),
        "sections": [name for name in order if name in present],
        "section_order": order,
        "facets": facet_counts(records),
        "records": records,
    }
    _write_json(bundle_dir / "index.json", index)
    detail_dir = bundle_dir / "detail"
    keep = set()
    for album in albums:
        name = f"{album['discogs_release_id']}.json"
        keep.add(name)
        _write_json(detail_dir / name, detail_record(conn, album))
    if detail_dir.is_dir():
        for stale in detail_dir.glob("*.json"):
            if stale.name not in keep:
                stale.unlink()
    return index


def read_index(bundle_dir: Path) -> dict[str, Any] | None:
    """The current index, or ``None`` when no sync has run."""
    path = bundle_dir / "index.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
