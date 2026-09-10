"""One sync: read the collection, derive the shelf data, cache art, enrich, write the bundle.

The steps, for each item in the Discogs collection:

1. Normalize the Discogs item into an album row.
2. Resolve the artist sort-name: the manual override, then the cache, then
   MusicBrainz. An artist MusicBrainz cannot match keeps the Discogs name as
   its sort-name for this run and is tried again next time. A classical
   release files under its performer, the last credited artist, because
   Discogs credits the composer first.
3. Derive the traits, the shelf section, the facets, and the primary facet.
4. Cache the thumbnail and the cover under the art directory, once.

Then remove any album that left the collection, fetch each release once for
its tracklist, country, and marketplace summary, and write the bundle. A
release that fails to fetch keeps what it had.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from joshua_vinyl import db
from joshua_vinyl.bundle import write_bundle
from joshua_vinyl.config import ShelfConfig
from joshua_vinyl.facets import facets_of, primary_facet
from joshua_vinyl.log import get_logger
from joshua_vinyl.musicbrainz import ArtistSort, strip_discogs_suffix
from joshua_vinyl.shelf import COMPILATION, SOUNDTRACK, section_for, traits_of

logger = get_logger("vinyl.sync")

SOURCE_OVERRIDE = "override"
SOURCE_MUSICBRAINZ = "musicbrainz"
SOURCE_UNRESOLVED = "unresolved"
META_LAST_SYNC = "last_sync"
META_LAST_RESULT = "last_result"
CLASSICAL = "Classical"


class CollectionSource(Protocol):
    """What the sync needs from Discogs: the items, one release, and image bytes."""

    def collection(self, username: str) -> Iterable[dict[str, Any]]: ...

    def release(self, release_id: int, *, currency: str | None = None) -> dict[str, Any]: ...

    def download(self, url: str) -> bytes: ...


class SortNameSource(Protocol):
    """What the sync needs from MusicBrainz."""

    def search_artist(self, name: str) -> ArtistSort | None: ...


@dataclass
class SyncResult:
    """What one run did."""

    started_at: str
    finished_at: str = ""
    count: int = 0
    removed: int = 0
    art_cached: int = 0
    art_failed: int = 0
    enriched: int = 0
    enrich_failed: int = 0
    unresolved_artists: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "count": self.count,
            "removed": self.removed,
            "art_cached": self.art_cached,
            "art_failed": self.art_failed,
            "enriched": self.enriched,
            "enrich_failed": self.enrich_failed,
            "unresolved_artists": list(self.unresolved_artists),
        }


def _join_artists(artists: list[dict[str, Any]]) -> str:
    """The credit line, with the Discogs join words: ``Bach / Glenn Gould``."""
    parts: list[str] = []
    for artist in artists:
        name = strip_discogs_suffix(artist.get("anv") or artist.get("name", ""))
        parts.append(name)
        join = (artist.get("join") or "").strip()
        if join:
            parts.append(join if join == "," else f" {join} ")
    text = "".join(part if part.strip() != "," else ", " for part in parts)
    return re.sub(r"\s+", " ", text).strip(" ,")


def normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    """Turn a collection item, or a full release, into the album fields.

    A collection item wraps the release in ``basic_information``. A full
    release from ``/releases/{id}`` has the same fields at the top level,
    with ``images`` in place of ``thumb`` and ``cover_image``.

    ``primary_artist`` is the first credit, which decides the compilation
    trait. ``sort_artist`` is the credit the shelf files under: the same,
    except that a classical release with more than one credit files under the
    last one, the performer, because Discogs credits the composer first.
    """
    info = item.get("basic_information") or item
    artists = info.get("artists") or []
    names = [strip_discogs_suffix(artist["name"]) for artist in artists]
    primary = names[0] if names else ""
    genres = list(info.get("genres") or [])
    sort_artist = names[-1] if CLASSICAL in genres and len(names) > 1 else primary
    labels = info.get("labels") or []
    label = labels[0].get("name") if labels else None
    catalog_no = labels[0].get("catno") if labels else None
    if catalog_no and catalog_no.strip().casefold() == "none":
        catalog_no = None
    formats = info.get("formats") or []
    format_text = None
    if formats:
        first = formats[0]
        descriptions = [str(d) for d in (first.get("descriptions") or [])]
        format_text = ", ".join([first.get("name", ""), *descriptions]).strip(", ")
    images = info.get("images") or []
    thumb_url = info.get("thumb") or (images[0].get("uri150") if images else None)
    cover_url = info.get("cover_image") or (images[0].get("uri") if images else None)
    year = info.get("year") or None
    return {
        "discogs_release_id": int(info["id"]),
        "instance_id": item.get("instance_id"),
        "master_id": int(info["master_id"]) if info.get("master_id") else None,
        "artist": _join_artists(artists) or primary,
        "primary_artist": primary,
        "sort_artist": sort_artist,
        "title": info.get("title", ""),
        "year": int(year) if year else None,
        "label": label,
        "catalog_no": catalog_no,
        "format": format_text or None,
        "formats": formats,
        "country": info.get("country"),
        "genres": genres,
        "styles": list(info.get("styles") or []),
        "thumb_url": thumb_url or None,
        "cover_url": cover_url or None,
        "added_at": item.get("date_added"),
    }


def tracks_of(release: dict[str, Any]) -> list[dict[str, Any]]:
    """The tracklist as flat rows: ``position``, ``title``, ``duration``.

    A heading or an index entry becomes a row with no position, and the
    sub-tracks of an index follow it. A track credited to its own artist (a
    compilation) keeps that credit in front of the title.
    """
    rows: list[dict[str, Any]] = []

    def add(track: dict[str, Any]) -> None:
        title = (track.get("title") or "").strip()
        credit = _join_artists(track.get("artists") or [])
        if credit:
            title = f"{credit} – {title}"
        rows.append(
            {
                "position": (track.get("position") or "").strip(),
                "title": title,
                "duration": (track.get("duration") or "").strip() or None,
            }
        )

    for track in release.get("tracklist") or []:
        add(track)
        for sub in track.get("sub_tracks") or []:
            add(sub)
    return rows


def resolve_artist_sort(
    conn: sqlite3.Connection,
    name: str,
    config: ShelfConfig,
    musicbrainz: SortNameSource | None,
    *,
    now: datetime,
) -> tuple[str, str]:
    """The sort-name for ``name`` and where it came from."""
    if name in config.overrides.artist_sort:
        return config.overrides.artist_sort[name], SOURCE_OVERRIDE
    cached = db.get_artist(conn, name)
    if cached:
        return cached["sort_name"], cached["source"]
    match = musicbrainz.search_artist(name) if musicbrainz else None
    if match is None:
        return name, SOURCE_UNRESOLVED
    db.put_artist(
        conn,
        name,
        match.sort_name,
        type_=match.type,
        source=SOURCE_MUSICBRAINZ,
        mbid=match.mbid,
        resolved_at=now.isoformat(timespec="seconds"),
    )
    return match.sort_name, SOURCE_MUSICBRAINZ


def cache_art(
    discogs: CollectionSource, art_dir: Path, release_id: int, url: str | None, suffix: str
) -> tuple[str | None, bool]:
    """Save one image under ``art_dir`` once. Return the bundle path and whether it downloaded."""
    if not url:
        return None, False
    name = f"{release_id}{suffix}.jpg"
    path = art_dir / name
    relative = f"art/{name}"
    if path.is_file():
        return relative, False
    art_dir.mkdir(parents=True, exist_ok=True)
    data = discogs.download(url)
    if not data:
        raise ValueError(f"empty image body for release {release_id}")
    # Written to a temporary name and moved, the way the bundle is. A file cut
    # short by a crash would otherwise count as cached for good.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return relative, True


def album_from_item(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    config: ShelfConfig,
    musicbrainz: SortNameSource | None,
    *,
    now: datetime,
) -> dict[str, Any]:
    """The album row for one item, with sort-name, section, and facets derived."""
    fields = normalize_item(item)
    release_id = fields["discogs_release_id"]
    key = str(release_id)
    sort_name, source = resolve_artist_sort(
        conn, fields["sort_artist"], config, musicbrainz, now=now
    )
    traits = traits_of(fields["primary_artist"], fields["styles"], fields["formats"])
    section = config.overrides.section.get(key) or section_for(sort_name, traits, config.sections)
    facets = facets_of(fields["genres"], fields["styles"], config.facets)
    facet = config.overrides.primary_facet.get(key) or primary_facet(
        fields["genres"], fields["styles"], config.facets
    )
    return {
        "discogs_release_id": release_id,
        "instance_id": fields["instance_id"],
        "master_id": fields["master_id"],
        "artist": fields["artist"],
        "artist_sort": sort_name,
        "artist_sort_source": source,
        "title": fields["title"],
        "year": fields["year"],
        "label": fields["label"],
        "catalog_no": fields["catalog_no"],
        "format": fields["format"],
        "country": fields["country"],
        "genres": fields["genres"],
        "styles": fields["styles"],
        "facets": facets,
        "primary_facet": facet,
        "is_compilation": COMPILATION in traits,
        "is_soundtrack": SOUNDTRACK in traits,
        "thumb_path": None,
        "art_path": None,
        "added_at": fields["added_at"],
        "shelf_section": section,
        "notes": None,
        "thumb_url": fields["thumb_url"],
        "cover_url": fields["cover_url"],
    }


def enrich_album(
    conn: sqlite3.Connection,
    discogs: CollectionSource,
    release_id: int,
    *,
    currency: str,
    now: datetime,
) -> None:
    """Fetch one release and store its tracks, its country, and its market price."""
    release = discogs.release(release_id, currency=currency)
    db.replace_tracks(conn, release_id, tracks_of(release))
    country = release.get("country")
    if country:
        db.set_album_country(conn, release_id, country)
    db.put_price(
        conn,
        release_id,
        lowest_price=release.get("lowest_price"),
        currency=currency,
        num_for_sale=release.get("num_for_sale"),
        checked_at=now.isoformat(timespec="seconds"),
    )


def run_sync(
    conn: sqlite3.Connection,
    *,
    discogs: CollectionSource,
    musicbrainz: SortNameSource | None,
    username: str,
    config: ShelfConfig,
    art_dir: Path,
    bundle_dir: Path,
    currency: str = "USD",
    enrich: bool = True,
    now: datetime | None = None,
) -> SyncResult:
    """Run one full sync and write the bundle. Commits as it goes."""
    started = now or datetime.now(UTC)
    result = SyncResult(started_at=started.isoformat(timespec="seconds"))
    try:
        return _run(
            conn,
            discogs=discogs,
            musicbrainz=musicbrainz,
            username=username,
            config=config,
            art_dir=art_dir,
            bundle_dir=bundle_dir,
            currency=currency,
            enrich=enrich,
            now=now,
            started=started,
            result=result,
        )
    except Exception as error:
        # The status route reads these two keys. Without this, a revoked token
        # leaves "ok" and a stale time in place for as long as it keeps failing.
        db.set_meta(conn, META_LAST_SYNC, datetime.now(UTC).isoformat(timespec="seconds"))
        db.set_meta(conn, META_LAST_RESULT, f"error: {error}"[:200])
        conn.commit()
        logger.error({"message": "sync failed", "error": str(error)[:200]})
        raise


def _run(
    conn: sqlite3.Connection,
    *,
    discogs: CollectionSource,
    musicbrainz: SortNameSource | None,
    username: str,
    config: ShelfConfig,
    art_dir: Path,
    bundle_dir: Path,
    currency: str,
    enrich: bool,
    now: datetime | None,
    started: datetime,
    result: SyncResult,
) -> SyncResult:
    """The sync itself. ``run_sync`` wraps it to record a failure."""
    # A collection is keyed by instance, so one release can appear twice. The
    # keys of a dict keep the order and drop the repeat, so the count is right
    # and the enrich loop fetches each release once.
    seen: dict[int, None] = {}
    for item in discogs.collection(username):
        album = album_from_item(conn, item, config, musicbrainz, now=started)
        release_id = album["discogs_release_id"]
        seen[release_id] = None
        if album["artist_sort_source"] == SOURCE_UNRESOLVED:
            result.unresolved_artists.append(album["artist"])
        thumb_url = album.pop("thumb_url")
        cover_url = album.pop("cover_url")
        for key, url, suffix in (("thumb_path", thumb_url, "-thumb"), ("art_path", cover_url, "")):
            try:
                path, downloaded = cache_art(discogs, art_dir, release_id, url, suffix)
            except Exception:
                logger.warning({"message": "art download failed", "release_id": release_id})
                result.art_failed += 1
                path, downloaded = None, False
            album[key] = path
            result.art_cached += int(downloaded)
        db.upsert_album(conn, album)
        conn.commit()
    result.removed = db.delete_albums_not_in(conn, set(seen))
    result.count = len(seen)
    conn.commit()
    if enrich:
        for release_id in seen:
            try:
                enrich_album(conn, discogs, release_id, currency=currency, now=started)
                result.enriched += 1
            except Exception:
                logger.warning({"message": "release fetch failed", "release_id": release_id})
                result.enrich_failed += 1
            conn.commit()
    finished = datetime.now(UTC) if now is None else started
    result.finished_at = finished.isoformat(timespec="seconds")
    db.set_meta(conn, META_LAST_SYNC, result.finished_at)
    db.set_meta(conn, META_LAST_RESULT, "ok")
    conn.commit()
    write_bundle(conn, bundle_dir, config, now=finished)
    logger.info({"message": "sync finished", **result.as_dict()})
    return result
