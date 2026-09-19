"""Reading a record: the checks that decide whether a candidate is the record."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import load_fixture
from joshua_vinyl import identify


@pytest.fixture
def picture_disc() -> dict[str, Any]:
    return load_fixture("discogs_release_coheed_7590859.json")


@pytest.fixture
def abbey_road() -> dict[str, Any]:
    return load_fixture("discogs_release_beatles_abbey_road.json")


def test_a_good_barcode_passes_its_check_digit() -> None:
    assert identify.barcode_is_consistent("602567725817") is True
    assert identify.barcode_is_consistent("0 602567725817") is True


def test_a_misread_barcode_fails() -> None:
    assert identify.barcode_is_consistent("602567725818") is False


def test_a_barcode_with_no_check_digit_says_nothing() -> None:
    assert identify.barcode_is_consistent("0 7863-55336-1") is None
    assert identify.barcode_is_consistent("") is None


def test_the_format_line_keeps_every_descriptor(picture_disc: dict[str, Any]) -> None:
    assert identify.format_text(picture_disc["formats"]) == (
        "Vinyl, LP, Album, Limited Edition, Picture Disc"
    )


def test_a_format_line_counts_a_double_album(abbey_road: dict[str, Any]) -> None:
    formats = [{"name": "Vinyl", "qty": "2", "descriptions": ["LP", "Album"]}]
    assert identify.format_text(formats) == "2 x Vinyl, LP, Album"


def test_a_picture_disc_is_flagged(picture_disc: dict[str, Any]) -> None:
    assert identify.oddities(picture_disc) == ["picture disc"]


def test_an_ordinary_pressing_is_not_flagged(abbey_road: dict[str, Any]) -> None:
    assert identify.oddities(abbey_road) == []


def test_the_notes_are_read_for_what_the_format_line_hides() -> None:
    release = {"notes": "DEMONSTRATION - Not For Sale. Promotional copy."}
    flags = identify.note_flags(release)
    assert "demonstration" in flags
    assert "not for sale" in flags


def test_a_tracklist_that_matches_falsifies_nothing(abbey_road: dict[str, Any]) -> None:
    titles = [track["title"] for track in abbey_road["tracklist"]]
    result = identify.compare_tracks(titles, abbey_road)
    assert result["all_present"] is True
    assert result["covers_whole_release"] is True


def test_one_side_matches_a_subset(abbey_road: dict[str, Any]) -> None:
    titles = [track["title"] for track in abbey_road["tracklist"][:4]]
    result = identify.compare_tracks(titles, abbey_road)
    assert result["all_present"] is True
    assert result["covers_whole_release"] is False


def test_a_track_the_release_does_not_have_is_named(abbey_road: dict[str, Any]) -> None:
    result = identify.compare_tracks(["Sunday Morning"], abbey_road)
    assert result["all_present"] is False
    assert result["missing_from_release"] == ["Sunday Morning"]


def test_no_tracklist_read_means_no_comparison(abbey_road: dict[str, Any]) -> None:
    assert identify.compare_tracks([], abbey_road) is None


def test_the_label_pictures_come_from_the_secondary_images(abbey_road: dict[str, Any]) -> None:
    urls = identify.label_image_urls(abbey_road)
    assert urls
    assert all(url.startswith("http") for url in urls)


def test_a_candidate_carries_the_evidence(picture_disc: dict[str, Any]) -> None:
    candidate = identify.summarize(
        picture_disc,
        found_by="catalog number",
        level="album",
        owned_release_ids={picture_disc["id"]},
        owned_master_ids=set(),
        tracks_read=["Welcome Home"],
    )
    assert candidate["release_id"] == picture_disc["id"]
    assert candidate["format_warnings"] == ["picture disc"]
    assert candidate["already_in_collection"] is True
    assert candidate["level"] == "album"
    assert candidate["found_by"] == "catalog number"
    assert candidate["tracks"]["read"] == 1


def test_a_candidate_says_when_the_album_is_owned_in_another_pressing(
    abbey_road: dict[str, Any],
) -> None:
    candidate = identify.summarize(
        abbey_road,
        found_by="barcode",
        level="pressing",
        owned_release_ids=set(),
        owned_master_ids={abbey_road["master_id"]},
    )
    assert candidate["already_in_collection"] is False
    assert candidate["same_album_in_collection"] is True


def test_an_unconfirmed_pressing_says_so_at_the_front_of_the_note() -> None:
    note = identify.build_note("Yard sale, Maplewood.", pressing_confirmed=False)
    assert note.startswith(identify.UNCONFIRMED)
    assert "Yard sale" in note


def test_a_confirmed_pressing_keeps_the_note_as_written() -> None:
    assert identify.build_note("Barcode read.", pressing_confirmed=True) == "Barcode read."


def test_an_unconfirmed_pressing_with_no_note_is_the_marker_alone() -> None:
    assert identify.build_note(None, pressing_confirmed=False) == identify.UNCONFIRMED


def test_a_long_note_is_cut_to_what_discogs_accepts() -> None:
    long_text = "word " * 200
    note = identify.build_note(long_text, pressing_confirmed=False)
    assert len(note) <= identify.NOTE_LIMIT
    assert note.startswith(identify.UNCONFIRMED)
    assert note.endswith("…")


def test_a_long_note_on_a_confirmed_pressing_is_cut_too() -> None:
    note = identify.build_note("word " * 200, pressing_confirmed=True)
    assert len(note) <= identify.NOTE_LIMIT


def test_a_release_with_no_secondary_image_falls_back_to_what_it_has() -> None:
    release = {"images": [{"type": "primary", "uri": "http://example/1.jpg"}]}
    assert identify.label_image_urls(release) == ["http://example/1.jpg"]


def test_a_release_with_no_image_at_all_has_no_label_picture() -> None:
    assert identify.label_image_urls({}) == []


def test_a_candidate_falls_back_to_the_sort_credit() -> None:
    release = {"id": 5, "artists": [], "artists_sort": "Various", "title": "A Compilation"}
    candidate = identify.summarize(release, found_by="catalog number", level="album")
    assert candidate["artist"] == "Various"


def test_a_note_on_a_confirmed_pressing_is_cut_at_the_limit() -> None:
    note = identify.build_note("x" * 400, pressing_confirmed=True)
    assert len(note) == identify.NOTE_LIMIT
