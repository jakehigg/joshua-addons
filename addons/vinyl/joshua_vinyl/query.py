"""Questions about the collection, answered from the static bundle.

The browse interface reads the bundle, and so do the tools. Nothing here
touches Discogs or the database, so an answer costs one file read and stays
correct while the network is down. The index holds every record; a detail
file holds the tracklist and the price of one record.

A search is tolerant on purpose. A person says "Zeppelin", "led zeppelin",
or "Beatles" for "The Beatles", and each must find the record.
"""

from __future__ import annotations

import json
import random
import unicodedata
from pathlib import Path
from typing import Any

BRIEF_FIELDS = ("id", "title", "artist", "year", "label", "section", "facet", "decade")
MAX_LIMIT = 50
LEADING_WORDS = ("the ", "a ", "an ")


def normalize(text: str) -> str:
    """Fold a name for comparison: no accents, no case, no leading article."""
    folded = "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    ).casefold()
    folded = " ".join(folded.replace("&", "and").split())
    for word in LEADING_WORDS:
        if folded.startswith(word):
            return folded[len(word) :]
    return folded


def brief(record: dict[str, Any]) -> dict[str, Any]:
    """The short form of a record, for a list in a chat message."""
    return {field: record[field] for field in BRIEF_FIELDS if record.get(field) is not None}


def _score(record: dict[str, Any], needle: str) -> int:
    """How well one record answers a query. 0 is no match."""
    title = normalize(record.get("title", ""))
    artist = normalize(record.get("artist", ""))
    label = normalize(record.get("label", ""))
    if needle in (title, artist):
        return 100
    if title.startswith(needle) or artist.startswith(needle):
        return 80
    if needle in title or needle in artist:
        return 60
    if needle in label:
        return 30
    return 0


def _matches_filters(
    record: dict[str, Any],
    *,
    genre: str | None,
    decade: int | None,
    section: str | None,
    year: int | None,
) -> bool:
    if genre is not None:
        wanted = normalize(genre)
        names = [*record.get("facets", []), *record.get("genres", []), *record.get("styles", [])]
        if not any(normalize(name) == wanted for name in names):
            return False
    if decade is not None and record.get("decade") != decade:
        return False
    if year is not None and record.get("year") != year:
        return False
    if section is not None and normalize(record.get("section", "")) != normalize(section):
        return False
    return True


def search(
    index: dict[str, Any],
    query: str | None = None,
    *,
    genre: str | None = None,
    decade: int | None = None,
    section: str | None = None,
    year: int | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """The records that answer a query, best first, with the number found.

    An empty query with a filter lists that part of the collection. A query
    matches the title, the artist, or the label.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    needle = normalize(query) if query else ""
    found: list[tuple[int, dict[str, Any]]] = []
    for record in index.get("records", []):
        if not _matches_filters(record, genre=genre, decade=decade, section=section, year=year):
            continue
        score = _score(record, needle) if needle else 1
        if score:
            found.append((score, record))
    found.sort(
        key=lambda pair: (-pair[0], normalize(pair[1].get("artist", "")), pair[1].get("year") or 0)
    )
    return {
        "found": len(found),
        "shown": min(len(found), limit),
        "records": [brief(record) for _, record in found[:limit]],
    }


def detail(bundle_dir: Path, record_id: int) -> dict[str, Any] | None:
    """The full record: the shelf section, the tracklist, and the last price."""
    path = bundle_dir / "detail" / f"{int(record_id)}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _counts(records: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        value = record.get(field)
        if value is None:
            continue
        counts[str(value)] = counts.get(str(value), 0) + 1
    return dict(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])))


def stats(index: dict[str, Any]) -> dict[str, Any]:
    """The shape of the collection: how many, of what, from when, and where."""
    records = index.get("records", [])
    years = sorted(record["year"] for record in records if record.get("year"))
    sections = _counts(records, "section")
    return {
        "records": index.get("count", len(records)),
        "bundle_generated_at": index.get("generated_at"),
        "genres": [
            {"name": facet["name"], "records": facet["count"]} for facet in index.get("facets", [])
        ],
        "decades": _counts(records, "decade"),
        "sections": sections,
        "fullest_sections": list(sections)[:3],
        "oldest_year": years[0] if years else None,
        "newest_year": years[-1] if years else None,
    }


def recent(index: dict[str, Any], limit: int = 10) -> dict[str, Any]:
    """The records added most recently, newest first."""
    limit = max(1, min(limit, MAX_LIMIT))
    records = [record for record in index.get("records", []) if record.get("added_at")]
    records.sort(key=lambda record: record["added_at"], reverse=True)
    return {
        "records": [{**brief(record), "added_at": record["added_at"]} for record in records[:limit]]
    }


def _reason(record: dict[str, Any], *, genre: str | None) -> str:
    """Why this record, in one sentence, from what the record says."""
    parts: list[str] = []
    facet = record.get("facet") or genre
    if facet:
        parts.append(str(facet))
    if record.get("year"):
        parts.append(f"from {record['year']}")
    body = ", ".join(parts) if parts else "in the collection"
    return (
        f"{record.get('title', 'This record')} is {body}. It is in section {record.get('section')}."
    )


def pick(
    index: dict[str, Any],
    *,
    genre: str | None = None,
    decade: int | None = None,
    section: str | None = None,
    exclude: list[int] | None = None,
    avoid: set[int] | None = None,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """One record to play, with the reason and the shelf section.

    The choice is always a record in the collection. A record that is out
    with somebody is never suggested, because it is not on the shelf.

    ``avoid`` holds the records suggested recently. They are left out while
    anything else fits, so the same record does not come up twice in a
    fortnight. When they are all that is left, one of them is suggested and
    the answer says the collection is smaller than the memory.
    """
    skip = set(exclude or [])
    pool = [
        record
        for record in index.get("records", [])
        if _matches_filters(record, genre=genre, decade=decade, section=section, year=None)
        and record["id"] not in skip
        and not record.get("lent")
    ]
    if not pool:
        return {"record": None, "pool": 0, "message": "No record in the collection matches that."}
    recent = avoid or set()
    fresh = [record for record in pool if record["id"] not in recent]
    repeat = not fresh
    chosen = (rng or random.Random()).choice(fresh or pool)
    answer = {
        "record": brief(chosen),
        "pool": len(fresh or pool),
        "reason": _reason(chosen, genre=genre),
    }
    if repeat:
        answer["repeat"] = True
        answer["message"] = (
            "Every record that fits was suggested recently, so this one comes up again."
        )
    return answer


def owned(
    index: dict[str, Any],
    *,
    artist: str | None = None,
    title: str | None = None,
    release_id: int | None = None,
    master_id: int | None = None,
) -> dict[str, Any]:
    """Is this album in the collection already? The question asked in a shop.

    Four ways to be sure, in order: the release id, the master id (the same
    album in another pressing), the artist and title together, and the title
    alone. The last one is a maybe, never a yes, because two records can
    share a title.
    """
    records = index.get("records", [])
    wanted_title = normalize(title) if title else None
    wanted_artist = normalize(artist) if artist else None

    def answer(how: str, found: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "owned": bool(found),
            "how": how,
            "copies": [brief(record) for record in found[:MAX_LIMIT]],
        }

    if release_id:
        found = [record for record in records if record.get("id") == int(release_id)]
        if found:
            return answer("the same pressing", found)
    if master_id:
        found = [record for record in records if record.get("master") == int(master_id)]
        if found:
            return answer("the same album, a different pressing", found)
    if wanted_title and wanted_artist:
        found = [
            record
            for record in records
            if normalize(record.get("title", "")) == wanted_title
            and wanted_artist in normalize(record.get("artist", ""))
        ]
        if found:
            return answer("the artist and the title", found)
    if wanted_title:
        found = [record for record in records if wanted_title in normalize(record.get("title", ""))]
        if found:
            result = answer("the title alone, so check the artist", found)
            result["owned"] = False
            result["maybe"] = True
            return result
    return {"owned": False, "how": "nothing matched", "copies": []}
