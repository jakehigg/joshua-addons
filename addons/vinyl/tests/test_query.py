"""The questions the tools answer, over a bundle from the fixtures."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest
from joshua_vinyl import query
from joshua_vinyl.bundle import read_index


@pytest.fixture
def index(synced: Path) -> dict[str, Any]:
    result = read_index(synced / "bundle")
    assert result is not None
    return result


def _titles(result: dict[str, Any]) -> list[str]:
    return [record["title"] for record in result["records"]]


def _artists(result: dict[str, Any]) -> list[str]:
    return [record["artist"] for record in result["records"]]


def test_normalize_drops_the_article_the_accent_and_the_case() -> None:
    assert query.normalize("The Beatles") == "beatles"
    assert query.normalize("Björk") == "bjork"
    assert query.normalize("Simon & Garfunkel") == "simon and garfunkel"


def test_search_finds_a_band_without_its_leading_the(index: dict[str, Any]) -> None:
    result = query.search(index, "beatles")
    assert result["found"] == 1
    assert _artists(result) == ["The Beatles"]


def test_search_matches_a_title(index: dict[str, Any]) -> None:
    result = query.search(index, "abbey road")
    assert _titles(result) == ["Abbey Road"]


def test_search_puts_an_exact_match_first(index: dict[str, Any]) -> None:
    result = query.search(index, "bob dylan")
    assert result["found"] >= 1
    assert result["records"][0]["artist"] == "Bob Dylan"


def test_search_with_no_query_lists_a_filtered_part(index: dict[str, Any]) -> None:
    result = query.search(index, None, decade=1960, limit=50)
    assert result["found"] > 0
    assert all(record["decade"] == 1960 for record in result["records"])


def test_search_by_genre_uses_the_facet_the_style_or_the_genre(index: dict[str, Any]) -> None:
    by_facet = query.search(index, None, genre="Rock", limit=50)
    assert by_facet["found"] > 0
    assert all(record["facet"] == "Rock" for record in by_facet["records"])


def test_search_by_section_finds_the_shelf(index: dict[str, Any]) -> None:
    beatles = query.search(index, "abbey road")["records"][0]
    result = query.search(index, None, section=beatles["section"], limit=50)
    assert beatles["id"] in [record["id"] for record in result["records"]]


def test_search_reports_the_number_found_and_the_number_shown(index: dict[str, Any]) -> None:
    result = query.search(index, None, limit=2)
    assert result["found"] == index["count"]
    assert result["shown"] == 2
    assert len(result["records"]) == 2


def test_search_caps_the_limit(index: dict[str, Any]) -> None:
    result = query.search(index, None, limit=10_000)
    assert len(result["records"]) <= query.MAX_LIMIT


def test_search_for_something_absent_finds_nothing(index: dict[str, Any]) -> None:
    result = query.search(index, "a record the house does not own")
    assert result == {"found": 0, "shown": 0, "records": []}


def test_a_brief_record_names_the_shelf_section(index: dict[str, Any]) -> None:
    record = query.search(index, "abbey road")["records"][0]
    assert record["section"] == "B"
    assert set(record) <= set(query.BRIEF_FIELDS)


def test_detail_gives_the_tracklist(synced: Path, index: dict[str, Any]) -> None:
    record_id = query.search(index, "abbey road")["records"][0]["id"]
    record = query.detail(synced / "bundle", record_id)
    assert record is not None
    assert record["title"] == "Abbey Road"
    assert record["tracks"]


def test_detail_of_a_record_not_in_the_collection_is_none(synced: Path) -> None:
    assert query.detail(synced / "bundle", 1) is None


def test_stats_counts_the_collection(index: dict[str, Any]) -> None:
    result = query.stats(index)
    assert result["records"] == index["count"]
    assert sum(result["sections"].values()) == index["count"]
    assert result["genres"]
    assert result["oldest_year"] <= result["newest_year"]
    assert len(result["fullest_sections"]) <= 3


def test_recent_lists_the_newest_first(index: dict[str, Any]) -> None:
    result = query.recent(index, limit=3)
    added = [record["added_at"] for record in result["records"]]
    assert added == sorted(added, reverse=True)
    assert len(added) == 3


def test_pick_chooses_a_record_the_house_owns(index: dict[str, Any]) -> None:
    result = query.pick(index, rng=random.Random(0))
    ids = [record["id"] for record in index["records"]]
    assert result["record"]["id"] in ids
    assert result["pool"] == index["count"]
    assert result["record"]["section"] in result["reason"]


def test_pick_honors_a_genre(index: dict[str, Any]) -> None:
    result = query.pick(index, genre="Rock", rng=random.Random(1))
    assert result["record"] is not None
    assert result["pool"] < index["count"]


def test_pick_leaves_out_an_excluded_record(index: dict[str, Any]) -> None:
    first = query.pick(index, rng=random.Random(2))["record"]["id"]
    result = query.pick(index, exclude=[first], rng=random.Random(2))
    assert result["record"]["id"] != first
    assert result["pool"] == index["count"] - 1


def test_pick_with_no_match_suggests_nothing(index: dict[str, Any]) -> None:
    result = query.pick(index, genre="Polka")
    assert result["record"] is None
    assert result["pool"] == 0
    assert "No record" in result["message"]


MADE_UP = {
    "count": 2,
    "generated_at": "2026-01-01T00:00:00+00:00",
    "facets": [],
    "records": [
        {
            "id": 1,
            "title": "Blue Lines",
            "artist": "Massive Attack",
            "label": "Wild Bunch Records",
            "year": 1991,
            "decade": 1990,
            "facet": "Electronic",
            "section": "M",
        },
        {
            "id": 2,
            "title": "A Private Pressing",
            "artist": "No One",
            "section": "N",
            "genres": ["Folk"],
        },
    ],
}


def test_search_matches_part_of_a_title() -> None:
    assert query.search(MADE_UP, "blue")["records"][0]["id"] == 1


def test_search_matches_the_middle_of_a_name() -> None:
    assert query.search(MADE_UP, "attack")["records"][0]["id"] == 1


def test_search_matches_a_label() -> None:
    result = query.search(MADE_UP, "wild bunch")
    assert result["found"] == 1
    assert result["records"][0]["id"] == 1


def test_search_by_year_is_exact() -> None:
    assert query.search(MADE_UP, None, year=1991)["found"] == 1
    assert query.search(MADE_UP, None, year=1992)["found"] == 0


def test_the_reason_falls_back_to_what_the_record_gives() -> None:
    result = query.pick(MADE_UP, genre="Folk", rng=random.Random(0))
    assert result["record"]["id"] == 2
    assert "Folk" in result["reason"]
    assert "section N" in result["reason"]
