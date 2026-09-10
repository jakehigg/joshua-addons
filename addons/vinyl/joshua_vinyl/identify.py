"""Identify one record from what a person reads off the sleeve or the label.

A person photographs a record and reads it. This module does the mechanical
half of what comes next: it asks Discogs for the candidates, and it surfaces
the things a reader forgets to check. It never reads a photograph, and it
never chooses. The choice needs a look at the candidate label images, and
that stays with the person, or with an agent that can see them.

The order of the searches is the order of the evidence, strongest first: a
barcode, then a catalog number, then the artist and the title. A barcode
carries a check digit, so a misread is arithmetic; a barcode that fails the
check is dropped, because a wrong barcode matches nothing and the answer
then falls through to a weaker guess that wears the clothes of a certain one.

Two levels of answer, and the difference must survive into the collection
note. ``pressing`` means one release is named. ``album`` means the album is
certain and the pressing is a guess.
"""

from __future__ import annotations

from typing import Any

from joshua_vinyl.query import normalize

NOTE_LIMIT = 255
UNCONFIRMED = "pressing-unconfirmed."

# A format descriptor that is rarely the record in the person's hand. Each one
# cost a wrong record before it was listed here.
ODDITIES = (
    "picture disc",
    "test pressing",
    "quadraphonic",
    "club edition",
    "promo",
    "white label",
    "acetate",
    "flexi-disc",
    "unofficial release",
)

# A word in the release notes that changes what the record is. The format line
# stays ordinary while the notes say "DEMONSTRATION Not For Sale".
NOTE_WARNINGS = (
    "demonstration",
    "not for sale",
    "promotional",
    "promo copy",
    "audiophile",
    "coloured vinyl",
    "colored vinyl",
    "limited edition",
    "club edition",
    "reissue",
)


def barcode_is_consistent(raw: str) -> bool | None:
    """Does a barcode pass its own check digit? ``None`` when it has none.

    UPC-A (12 digits) and EAN-13 both carry one. A US sleeve of the 1980s
    often prints eleven digits and no check digit; nothing can be said about
    those, so they answer ``None`` and the catalog number carries the weight.
    """
    digits = [int(ch) for ch in str(raw) if ch.isdigit()]
    if len(digits) not in (12, 13):
        return None
    check = digits.pop()
    if len(digits) == 11:
        total = sum(value * (3 if index % 2 == 0 else 1) for index, value in enumerate(digits))
    else:
        total = sum(value * (1 if index % 2 == 0 else 3) for index, value in enumerate(digits))
    return (10 - total % 10) % 10 == check


def format_text(formats: list[dict[str, Any]]) -> str:
    """The full format line, descriptors and all, for a person to read."""
    parts: list[str] = []
    for entry in formats or []:
        name = entry.get("name") or ""
        descriptions = [str(item) for item in (entry.get("descriptions") or [])]
        text = ", ".join([name, *descriptions]).strip(", ")
        quantity = entry.get("qty")
        if quantity and str(quantity) not in ("1", ""):
            text = f"{quantity} x {text}"
        if text:
            parts.append(text)
    return " + ".join(parts)


def oddities(release: dict[str, Any]) -> list[str]:
    """The format descriptors that say this is not an ordinary copy."""
    text = format_text(release.get("formats") or []).casefold()
    return [word for word in ODDITIES if word in text]


def note_flags(release: dict[str, Any]) -> list[str]:
    """The words in the release notes that change what the record is."""
    notes = (release.get("notes") or "").casefold()
    return [word for word in NOTE_WARNINGS if word in notes]


def compare_tracks(read: list[str], release: dict[str, Any]) -> dict[str, Any] | None:
    """Compare the titles a person read against the release.

    The tracklist is not an identifier. It is the one fact a wrong match
    cannot fake, so it falsifies a candidate and never chooses one. A reading
    of one side matches a subset, and that is reported, not failed.
    """
    if not read:
        return None
    theirs = [
        normalize(track.get("title", ""))
        for track in release.get("tracklist") or []
        if track.get("title")
    ]
    mine = [normalize(title) for title in read if title]
    missing = [
        title for title, folded in zip(read, mine, strict=False) if folded and folded not in theirs
    ]
    return {
        "read": len(mine),
        "on_release": len(theirs),
        "missing_from_release": missing,
        "all_present": not missing,
        "covers_whole_release": not missing and len(mine) == len(theirs),
    }


def _label_fields(release: dict[str, Any]) -> tuple[str | None, str | None]:
    labels = release.get("labels") or []
    if not labels:
        return None, None
    catalog = labels[0].get("catno")
    if catalog and catalog.strip().casefold() == "none":
        catalog = None
    return labels[0].get("name"), catalog


def label_image_urls(release: dict[str, Any], limit: int = 3) -> list[str]:
    """The candidate's disc-label pictures.

    A label is almost always a secondary image, and often the third to the
    seventh, so they are taken by type and not by position.
    """
    urls = [
        image["uri"]
        for image in release.get("images") or []
        if image.get("type") == "secondary" and image.get("uri")
    ]
    if not urls:
        urls = [image["uri"] for image in release.get("images") or [] if image.get("uri")]
    return urls[:limit]


def summarize(
    release: dict[str, Any],
    *,
    found_by: str,
    level: str,
    owned_release_ids: set[int] | None = None,
    owned_master_ids: set[int] | None = None,
    tracks_read: list[str] | None = None,
) -> dict[str, Any]:
    """One candidate, with the evidence a person has to read before choosing."""
    label, catalog_no = _label_fields(release)
    master_id = release.get("master_id") or None
    artists = release.get("artists") or []
    candidate: dict[str, Any] = {
        "release_id": int(release["id"]),
        "master_id": master_id,
        "artist": ", ".join(artist.get("name", "") for artist in artists).strip(", ")
        or release.get("artists_sort"),
        "title": release.get("title"),
        "year": release.get("year") or None,
        "country": release.get("country"),
        "label": label,
        "catalog_no": catalog_no,
        "format": format_text(release.get("formats") or []),
        "found_by": found_by,
        "level": level,
    }
    if release.get("notes"):
        candidate["notes"] = release["notes"][:600]
    warnings = oddities(release)
    if warnings:
        candidate["format_warnings"] = warnings
    warnings = note_flags(release)
    if warnings:
        candidate["note_warnings"] = warnings
    tracks = compare_tracks(tracks_read or [], release)
    if tracks is not None:
        candidate["tracks"] = tracks
    if owned_release_ids is not None:
        candidate["already_in_collection"] = int(release["id"]) in owned_release_ids
    if owned_master_ids is not None and master_id:
        candidate["same_album_in_collection"] = int(master_id) in owned_master_ids
    candidate["label_images"] = len(label_image_urls(release, limit=99))
    return candidate


def build_note(free_text: str | None, *, pressing_confirmed: bool) -> str:
    """The collection note, inside the 255 characters Discogs accepts.

    A longer value is refused with a 422 after the record is already in the
    collection, which reads exactly like a failed add. The caveat goes first,
    because only the front of a cut note survives.
    """
    head = "" if pressing_confirmed else UNCONFIRMED
    free = (free_text or "").strip()
    if not free:
        return head
    if not head:
        return free[:NOTE_LIMIT].strip()
    room = NOTE_LIMIT - len(head) - 1
    if len(free) <= room:
        return f"{head} {free}"
    cut = free[: max(0, room - 1)]
    space = cut.rfind(" ")
    trimmed = cut[:space] if space > 0 else cut
    return f"{head} {trimmed}…"
