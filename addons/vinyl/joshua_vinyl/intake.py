"""One record at a time: find it on Discogs, then put it in the collection.

This is the yard-sale path, not the batch path. A person holds one record,
photographs the label, and wants two answers: which release is this, and do
we own it already. The batch tool that filled the collection is a separate
program; what is here is the same knowledge, shaped for one or two records
and for an agent that can look at a picture.

Three rules decide the shape of this module.

**Nothing is written until a person confirms.** ``add_record`` plans by
default. The plan holds the release, the full format line, the release
notes, the duplicate check, and the exact note that would be written. A
second call with ``confirm`` does the write.

**A guess never becomes a certainty.** A pressing that is not confirmed
carries ``pressing-unconfirmed.`` at the front of its collection note, which
is the only place the difference survives.

**The record is browsable at once.** After a write the release goes into the
local database and the bundle is written again, so the new record is on the
shelf page without a sync.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from joshua_vinyl import db, identify
from joshua_vinyl.bundle import write_bundle
from joshua_vinyl.config import ShelfConfig
from joshua_vinyl.log import get_logger
from joshua_vinyl.sync import album_from_item, cache_art, enrich_album

logger = get_logger("vinyl.intake")

DEFAULT_FOLDER = 1
NOTES_FIELD = "notes"
MAX_CANDIDATES = 3


class DiscogsSource(Protocol):
    """The part of the Discogs client this module uses."""

    def search(self, **params: Any) -> list[dict[str, Any]]: ...

    def release(self, release_id: int, *, currency: str | None = None) -> dict[str, Any]: ...

    def download(self, url: str) -> bytes: ...

    def fields(self, username: str) -> list[dict[str, Any]]: ...

    def add_release(self, username: str, folder_id: int, release_id: int) -> dict[str, Any]: ...

    def set_field(
        self,
        username: str,
        folder_id: int,
        release_id: int,
        instance_id: int,
        field_id: int,
        value: str,
    ) -> None: ...


def _clean(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def search_plan(
    *,
    artist: str | None = None,
    title: str | None = None,
    catalog_no: str | None = None,
    barcode: str | None = None,
    label: str | None = None,
    year: int | None = None,
    country: str | None = None,
) -> list[dict[str, Any]]:
    """The searches to run, strongest evidence first.

    A barcode that fails its own check digit is dropped with a reason. It is
    a misread, not a rare pressing, and a wrong barcode finds nothing and
    then lets a weaker guess look certain.
    """
    plan: list[dict[str, Any]] = []
    barcode = _clean(barcode)
    if barcode:
        ok = identify.barcode_is_consistent(barcode)
        if ok is False:
            plan.append(
                {
                    "found_by": "barcode",
                    "skipped": "The barcode fails its own check digit, so it is a misread.",
                }
            )
        else:
            plan.append(
                {"found_by": "barcode", "level": "pressing", "params": {"barcode": barcode}}
            )
    catalog_no = _clean(catalog_no)
    if catalog_no:
        params: dict[str, Any] = {"catno": catalog_no}
        if _clean(artist):
            params["artist"] = artist
        plan.append({"found_by": "catalog number", "level": "album", "params": params})
    if _clean(artist) or _clean(title):
        params = {}
        if _clean(artist):
            params["artist"] = artist
        if _clean(title):
            params["release_title"] = title
        if label:
            params["label"] = label
        if year:
            params["year"] = year
        if country:
            params["country"] = country
        plan.append({"found_by": "artist and title", "level": "album", "params": params})
    return plan


def find_candidates(
    discogs: DiscogsSource,
    *,
    artist: str | None = None,
    title: str | None = None,
    catalog_no: str | None = None,
    barcode: str | None = None,
    label: str | None = None,
    year: int | None = None,
    country: str | None = None,
    tracks: list[str] | None = None,
    owned_release_ids: set[int] | None = None,
    owned_master_ids: set[int] | None = None,
    limit: int = MAX_CANDIDATES,
) -> dict[str, Any]:
    """At most three candidate releases, each with the evidence to judge it.

    Every candidate is read in full, because the four things that decide a
    record are not in a search result: the format descriptors, the release
    notes, the country, and the year against the label era.
    """
    plan = search_plan(
        artist=artist,
        title=title,
        catalog_no=catalog_no,
        barcode=barcode,
        label=label,
        year=year,
        country=country,
    )
    searched: list[dict[str, Any]] = []
    picked: list[tuple[int, str, str]] = []
    seen: set[int] = set()
    for step in plan:
        if "skipped" in step:
            searched.append({"by": step["found_by"], "skipped": step["skipped"]})
            continue
        results = discogs.search(**step["params"])
        searched.append({"by": step["found_by"], "results": len(results)})
        level = step["level"]
        if step["found_by"] == "barcode" and len(results) > 1:
            level = "album"
        for result in results:
            release_id = int(result["id"])
            if release_id in seen:
                continue
            seen.add(release_id)
            picked.append((release_id, step["found_by"], level))
            if len(picked) >= limit:
                break
        if len(picked) >= limit:
            break
    candidates = []
    for release_id, found_by, level in picked:
        release = discogs.release(release_id)
        candidates.append(
            identify.summarize(
                release,
                found_by=found_by,
                level=level,
                owned_release_ids=owned_release_ids,
                owned_master_ids=owned_master_ids,
                tracks_read=tracks,
            )
        )
    answer: dict[str, Any] = {"candidates": candidates, "searched": searched}
    if not candidates:
        answer["message"] = (
            "Discogs has no candidate for that reading. An empty answer is "
            "usually a different spelling of the artist, or a misread catalog "
            "number, not a record that is absent. Read the photograph again, "
            "then search the catalog number on its own."
        )
    return answer


def _notes_field_id(discogs: DiscogsSource, username: str) -> int | None:
    for field in discogs.fields(username):
        if str(field.get("name", "")).strip().casefold() == NOTES_FIELD:
            return int(field["id"])
    return None


def _summary(release: dict[str, Any]) -> dict[str, Any]:
    return identify.summarize(release, found_by="release id", level="pressing")


def add_record(
    conn: sqlite3.Connection,
    discogs: DiscogsSource,
    *,
    username: str,
    release_id: int,
    config: ShelfConfig,
    art_dir: Path,
    bundle_dir: Path,
    musicbrainz: Any = None,
    note: str | None = None,
    pressing_confirmed: bool = False,
    confirm: bool = False,
    allow_duplicate: bool = False,
    folder_id: int = DEFAULT_FOLDER,
    currency: str = "USD",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Plan an add, or make one. Nothing is written unless ``confirm`` is true."""
    started = now or datetime.now(UTC)
    release = discogs.release(release_id, currency=currency)
    summary = _summary(release)
    text = identify.build_note(note, pressing_confirmed=pressing_confirmed)
    owned_releases, owned_masters = db.collection_ids(conn)
    duplicate = release_id in owned_releases
    master_id = release.get("master_id") or None
    same_album = bool(master_id) and int(master_id) in owned_masters and not duplicate
    plan: dict[str, Any] = {
        "release": summary,
        "note": text,
        "already_in_collection": duplicate,
        "same_album_in_collection": same_album,
        "written": False,
    }
    if duplicate and not allow_duplicate:
        plan["refused"] = (
            "That release is already in the collection. Two copies of one "
            "album can be two pressings, so pass allow_duplicate to add a "
            "second copy on purpose."
        )
        return plan
    if not confirm:
        plan["confirm_to_write"] = (
            "Nothing is written yet. Read the format line, the notes, and the "
            "warnings above, then call again with confirm to add the record."
        )
        return plan

    added = discogs.add_release(username, folder_id, release_id)
    instance_id = added.get("instance_id")
    plan["written"] = True
    plan["instance_id"] = instance_id
    logger.info({"message": "record added", "release_id": release_id, "instance": instance_id})

    if text and instance_id:
        field_id = _notes_field_id(discogs, username)
        if field_id is None:
            plan["note_written"] = False
            plan["note_error"] = "The collection has no Notes field, so the note is not stored."
        else:
            try:
                discogs.set_field(username, folder_id, release_id, instance_id, field_id, text)
                plan["note_written"] = True
            except Exception as error:  # noqa: BLE001 — the add stands; the note did not
                plan["note_written"] = False
                plan["note_error"] = f"The record is in the collection and the note is not: {error}"
                logger.warning({"message": "note write failed", "release_id": release_id})

    item = dict(release)
    item["instance_id"] = instance_id
    item["date_added"] = started.isoformat(timespec="seconds")
    album = album_from_item(conn, item, config, musicbrainz, now=started)
    thumb_url = album.pop("thumb_url")
    cover_url = album.pop("cover_url")
    for key, url, suffix in (("thumb_path", thumb_url, "-thumb"), ("art_path", cover_url, "")):
        try:
            path, _ = cache_art(discogs, art_dir, release_id, url, suffix)
        except Exception:  # noqa: BLE001 — a missing cover is cosmetic
            logger.warning({"message": "art download failed", "release_id": release_id})
            path = None
        album[key] = path
    album["notes"] = text or None
    db.upsert_album(conn, album)
    conn.commit()
    try:
        enrich_album(conn, discogs, release_id, currency=currency, now=started)
    except Exception:  # noqa: BLE001 — the tracklist can wait for the nightly sync
        logger.warning({"message": "release fetch failed", "release_id": release_id})
    conn.commit()
    write_bundle(conn, bundle_dir, config, now=started)
    plan["shelf_section"] = album["shelf_section"]
    plan["on_the_shelf"] = True
    return plan
