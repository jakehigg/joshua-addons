"""One record at a time: the searches, the plan, and the guarded write."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    FakeMusicBrainz,
    FakeWriteDiscogs,
    collection_items,
    load_fixture,
    sync_into,
)
from joshua_vinyl import db, intake
from joshua_vinyl.bundle import read_index
from joshua_vinyl.config import ShelfConfig

PICTURE_DISC = 7590859
ABBEY_ROAD = 33300852


def _folsom_id() -> int:
    return int(load_fixture("discogs_release_cash_folsom.json")["id"])


@pytest.fixture
def without_folsom(data_dir: Path, fake_musicbrainz: FakeMusicBrainz) -> FakeWriteDiscogs:
    """A collection synced with every fixture record except the Johnny Cash one."""
    folsom = _folsom_id()
    items = [item for item in collection_items() if item["id"] != folsom]
    discogs = FakeWriteDiscogs(items)
    sync_into(data_dir, discogs, fake_musicbrainz)
    return discogs


def _add(
    data_dir: Path,
    discogs: FakeWriteDiscogs,
    musicbrainz: FakeMusicBrainz | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    conn = db.connect(data_dir / "vinyl.db")
    try:
        return intake.add_record(
            conn,
            discogs,
            username="example-user",
            config=ShelfConfig(),
            art_dir=data_dir / "art",
            bundle_dir=data_dir / "bundle",
            musicbrainz=musicbrainz,
            **kwargs,
        )
    finally:
        conn.close()


def test_the_searches_run_strongest_evidence_first() -> None:
    plan = intake.search_plan(
        artist="Bob Dylan", title="Highway 61", catalog_no="CS 9189", barcode="602567725817"
    )
    assert [step["found_by"] for step in plan] == ["barcode", "catalog number", "artist and title"]


def test_a_barcode_that_fails_its_check_digit_is_not_searched() -> None:
    plan = intake.search_plan(barcode="602567725818", catalog_no="CS 9189")
    assert "skipped" in plan[0]
    assert "misread" in plan[0]["skipped"]
    assert plan[1]["found_by"] == "catalog number"


def test_a_barcode_with_no_check_digit_is_searched_anyway() -> None:
    plan = intake.search_plan(barcode="0 7863-55336-1")
    assert plan[0]["params"]["barcode"] == "0 7863-55336-1"


def test_nothing_read_means_nothing_to_search() -> None:
    assert intake.search_plan() == []


def test_one_barcode_result_is_a_pressing(fake_write_discogs: FakeWriteDiscogs) -> None:
    fake_write_discogs.by("barcode", [ABBEY_ROAD])
    result = intake.find_candidates(fake_write_discogs, barcode="602567725817")
    assert result["candidates"][0]["level"] == "pressing"
    assert result["candidates"][0]["release_id"] == ABBEY_ROAD


def test_several_barcode_results_are_only_album_level(
    fake_write_discogs: FakeWriteDiscogs,
) -> None:
    fake_write_discogs.by("barcode", [ABBEY_ROAD, PICTURE_DISC])
    result = intake.find_candidates(fake_write_discogs, barcode="602567725817")
    assert [candidate["level"] for candidate in result["candidates"]] == ["album", "album"]


def test_at_most_three_candidates_come_back(fake_write_discogs: FakeWriteDiscogs) -> None:
    fake_write_discogs.by("catno", [7590859, 3940397, 1633083, 2337888])
    result = intake.find_candidates(fake_write_discogs, catalog_no="EVR 108")
    assert len(result["candidates"]) == 3


def test_a_candidate_carries_the_format_line_and_the_notes(
    fake_write_discogs: FakeWriteDiscogs,
) -> None:
    fake_write_discogs.by("catno", [PICTURE_DISC])
    result = intake.find_candidates(fake_write_discogs, catalog_no="EVR 108")
    candidate = result["candidates"][0]
    assert "Picture Disc" in candidate["format"]
    assert candidate["format_warnings"] == ["picture disc"]
    assert candidate["notes"]


def test_a_candidate_says_it_is_already_on_the_shelf(
    fake_write_discogs: FakeWriteDiscogs,
) -> None:
    fake_write_discogs.by("catno", [PICTURE_DISC])
    result = intake.find_candidates(
        fake_write_discogs, catalog_no="EVR 108", owned_release_ids={PICTURE_DISC}
    )
    assert result["candidates"][0]["already_in_collection"] is True


def test_the_tracklist_read_is_compared_against_each_candidate(
    fake_write_discogs: FakeWriteDiscogs,
) -> None:
    fake_write_discogs.by("catno", [ABBEY_ROAD])
    result = intake.find_candidates(
        fake_write_discogs, catalog_no="PCS 7088", tracks=["Come Together", "Something"]
    )
    assert result["candidates"][0]["tracks"]["all_present"] is True


def test_no_candidate_says_a_misread_is_more_likely_than_a_missing_record(
    fake_write_discogs: FakeWriteDiscogs,
) -> None:
    result = intake.find_candidates(fake_write_discogs, catalog_no="NOT A NUMBER")
    assert result["candidates"] == []
    assert "misread" in result["message"]


def test_the_searches_that_ran_are_reported(fake_write_discogs: FakeWriteDiscogs) -> None:
    fake_write_discogs.by("catno", [ABBEY_ROAD])
    result = intake.find_candidates(fake_write_discogs, catalog_no="PCS 7088", artist="The Beatles")
    assert result["searched"][0] == {"by": "catalog number", "results": 1}


def test_an_add_plans_and_writes_nothing(data_dir: Path, without_folsom: FakeWriteDiscogs) -> None:
    plan = _add(data_dir, without_folsom, release_id=_folsom_id())
    assert plan["written"] is False
    assert plan["note"].startswith("pressing-unconfirmed.")
    assert "confirm" in plan["confirm_to_write"]
    assert without_folsom.added == []


def test_a_planned_add_shows_the_release(data_dir: Path, without_folsom: FakeWriteDiscogs) -> None:
    plan = _add(data_dir, without_folsom, release_id=_folsom_id())
    assert plan["release"]["title"]
    assert plan["release"]["format"]
    assert plan["already_in_collection"] is False


def test_a_record_already_in_the_collection_is_refused(
    data_dir: Path, without_folsom: FakeWriteDiscogs
) -> None:
    plan = _add(data_dir, without_folsom, release_id=PICTURE_DISC, confirm=True)
    assert plan["written"] is False
    assert "already in the collection" in plan["refused"]
    assert without_folsom.added == []


def test_a_second_copy_is_added_on_purpose(
    data_dir: Path, without_folsom: FakeWriteDiscogs, fake_musicbrainz: FakeMusicBrainz
) -> None:
    plan = _add(
        data_dir,
        without_folsom,
        fake_musicbrainz,
        release_id=PICTURE_DISC,
        confirm=True,
        allow_duplicate=True,
    )
    assert plan["written"] is True
    assert without_folsom.added


def test_a_confirmed_add_writes_the_record_and_the_note(
    data_dir: Path, without_folsom: FakeWriteDiscogs, fake_musicbrainz: FakeMusicBrainz
) -> None:
    folsom = _folsom_id()
    plan = _add(
        data_dir,
        without_folsom,
        fake_musicbrainz,
        release_id=folsom,
        note="Yard sale, two dollars.",
        confirm=True,
    )
    assert plan["written"] is True
    assert plan["note_written"] is True
    assert without_folsom.added == [("example-user", 1, folsom)]
    release_id, field_id, value = without_folsom.notes[0]
    assert (release_id, field_id) == (folsom, 3)
    assert value.startswith("pressing-unconfirmed.")
    assert "Yard sale" in value


def test_a_confirmed_pressing_writes_no_caveat(
    data_dir: Path, without_folsom: FakeWriteDiscogs, fake_musicbrainz: FakeMusicBrainz
) -> None:
    _add(
        data_dir,
        without_folsom,
        fake_musicbrainz,
        release_id=_folsom_id(),
        note="Label matrix matches.",
        pressing_confirmed=True,
        confirm=True,
    )
    assert without_folsom.notes[0][2] == "Label matrix matches."


def test_the_new_record_is_on_the_shelf_at_once(
    data_dir: Path, without_folsom: FakeWriteDiscogs, fake_musicbrainz: FakeMusicBrainz
) -> None:
    folsom = _folsom_id()
    before = read_index(data_dir / "bundle")["count"]
    plan = _add(data_dir, without_folsom, fake_musicbrainz, release_id=folsom, confirm=True)
    index = read_index(data_dir / "bundle")
    assert index["count"] == before + 1
    assert plan["shelf_section"] == "C"
    assert plan["on_the_shelf"] is True
    assert folsom in [record["id"] for record in index["records"]]


def test_the_new_record_keeps_its_tracklist(
    data_dir: Path, without_folsom: FakeWriteDiscogs, fake_musicbrainz: FakeMusicBrainz
) -> None:
    folsom = _folsom_id()
    _add(data_dir, without_folsom, fake_musicbrainz, release_id=folsom, confirm=True)
    detail = json.loads(
        (data_dir / "bundle" / "detail" / f"{folsom}.json").read_text(encoding="utf-8")
    )
    assert detail["tracks"]


def test_a_failed_note_leaves_the_record_added_and_says_so(
    data_dir: Path, without_folsom: FakeWriteDiscogs, fake_musicbrainz: FakeMusicBrainz
) -> None:
    without_folsom.note_fails = True
    plan = _add(
        data_dir,
        without_folsom,
        fake_musicbrainz,
        release_id=_folsom_id(),
        note="A note.",
        confirm=True,
    )
    assert plan["written"] is True
    assert plan["note_written"] is False
    assert "is in the collection and the note is not" in plan["note_error"]


def test_a_collection_with_no_notes_field_says_so(
    data_dir: Path, without_folsom: FakeWriteDiscogs, fake_musicbrainz: FakeMusicBrainz
) -> None:
    without_folsom.collection_fields = [{"id": 1, "name": "Media Condition"}]
    plan = _add(data_dir, without_folsom, fake_musicbrainz, release_id=_folsom_id(), confirm=True)
    assert plan["note_written"] is False
    assert "no Notes field" in plan["note_error"]


def test_the_master_id_is_stored_so_a_second_pressing_is_recognized(
    data_dir: Path, without_folsom: FakeWriteDiscogs
) -> None:
    conn = db.connect(data_dir / "vinyl.db")
    try:
        releases, masters = db.collection_ids(conn)
    finally:
        conn.close()
    assert PICTURE_DISC in releases
    assert masters


def test_the_last_search_uses_every_field_that_was_read(
    fake_write_discogs: FakeWriteDiscogs,
) -> None:
    fake_write_discogs.by("release_title", [ABBEY_ROAD])
    intake.find_candidates(
        fake_write_discogs,
        artist="The Beatles",
        title="Abbey Road",
        label="Apple Records",
        year=1969,
        country="US",
    )
    params = fake_write_discogs.searches[-1]
    assert params["label"] == "Apple Records"
    assert params["year"] == 1969
    assert params["country"] == "US"


def test_a_misread_barcode_is_reported_and_the_next_search_still_runs(
    fake_write_discogs: FakeWriteDiscogs,
) -> None:
    fake_write_discogs.by("catno", [ABBEY_ROAD])
    result = intake.find_candidates(
        fake_write_discogs, barcode="602567725818", catalog_no="PCS 7088"
    )
    assert "misread" in result["searched"][0]["skipped"]
    assert result["candidates"][0]["release_id"] == ABBEY_ROAD


def test_one_release_found_twice_is_one_candidate(fake_write_discogs: FakeWriteDiscogs) -> None:
    fake_write_discogs.by("catno", [ABBEY_ROAD])
    fake_write_discogs.by("release_title", [ABBEY_ROAD])
    result = intake.find_candidates(
        fake_write_discogs, catalog_no="PCS 7088", artist="The Beatles", title="Abbey Road"
    )
    assert len(result["candidates"]) == 1


def test_a_missing_cover_does_not_stop_the_add(
    data_dir: Path, without_folsom: FakeWriteDiscogs, fake_musicbrainz: FakeMusicBrainz
) -> None:
    folsom = _folsom_id()
    release = without_folsom.releases[folsom]
    without_folsom.fail_urls = {image["uri"] for image in release["images"]}
    without_folsom.fail_urls |= {image["uri150"] for image in release["images"]}
    plan = _add(data_dir, without_folsom, fake_musicbrainz, release_id=folsom, confirm=True)
    assert plan["written"] is True
    assert plan["on_the_shelf"] is True


def test_a_release_that_cannot_be_read_again_still_lands_on_the_shelf(
    data_dir: Path, without_folsom: FakeWriteDiscogs, fake_musicbrainz: FakeMusicBrainz
) -> None:
    folsom = _folsom_id()
    calls: list[int] = []
    original = without_folsom.release

    def once(release_id: int, *, currency: str | None = None) -> dict[str, Any]:
        calls.append(release_id)
        if len(calls) > 1:
            raise RuntimeError("discogs timed out")
        return original(release_id, currency=currency)

    without_folsom.release = once  # type: ignore[method-assign]
    plan = _add(data_dir, without_folsom, fake_musicbrainz, release_id=folsom, confirm=True)
    assert plan["on_the_shelf"] is True


def _remove(
    data_dir: Path, discogs: FakeWriteDiscogs, release_id: int, **kwargs: Any
) -> dict[str, Any]:
    conn = db.connect(data_dir / "vinyl.db")
    try:
        return intake.remove_record(
            conn,
            discogs,
            username="example-user",
            release_id=release_id,
            config=ShelfConfig(),
            bundle_dir=data_dir / "bundle",
            **kwargs,
        )
    finally:
        conn.close()


def test_a_removal_plans_and_takes_nothing_out(
    data_dir: Path, without_folsom: FakeWriteDiscogs
) -> None:
    plan = _remove(data_dir, without_folsom, PICTURE_DISC)
    assert plan["written"] is False
    assert plan["copies"]
    assert "confirm" in plan["confirm_to_write"]
    assert without_folsom.removed == []


def test_removing_a_record_the_house_does_not_own_is_refused(
    data_dir: Path, without_folsom: FakeWriteDiscogs
) -> None:
    plan = _remove(data_dir, without_folsom, _folsom_id(), confirm=True)
    assert plan["written"] is False
    assert "nothing to remove" in plan["refused"]
    assert without_folsom.removed == []


def test_a_confirmed_removal_takes_the_record_off_the_shelf(
    data_dir: Path, without_folsom: FakeWriteDiscogs
) -> None:
    before = read_index(data_dir / "bundle")["count"]
    plan = _remove(data_dir, without_folsom, PICTURE_DISC, confirm=True)
    index = read_index(data_dir / "bundle")
    assert plan["written"] is True
    assert plan["off_the_shelf"] is True
    assert without_folsom.removed[0][0] == PICTURE_DISC
    assert index["count"] == before - 1
    assert PICTURE_DISC not in [record["id"] for record in index["records"]]


def test_two_copies_need_the_instance_said_out_loud(
    data_dir: Path, without_folsom: FakeWriteDiscogs
) -> None:
    without_folsom.copies[PICTURE_DISC].append(
        {
            "id": PICTURE_DISC,
            "instance_id": 999_001,
            "folder_id": 1,
            "date_added": "2026-02-02T10:00:00-07:00",
            "notes": [{"field_id": 3, "value": "the second copy"}],
        }
    )
    plan = _remove(data_dir, without_folsom, PICTURE_DISC, confirm=True)
    assert plan["written"] is False
    assert "more than one copy" in plan["refused"]
    assert len(plan["copies"]) == 2
    assert any(copy.get("note") == "the second copy" for copy in plan["copies"])
    assert without_folsom.removed == []


def test_one_copy_of_two_leaves_the_record_on_the_shelf(
    data_dir: Path, without_folsom: FakeWriteDiscogs
) -> None:
    without_folsom.copies[PICTURE_DISC].append(
        {"id": PICTURE_DISC, "instance_id": 999_001, "folder_id": 1, "notes": []}
    )
    plan = _remove(data_dir, without_folsom, PICTURE_DISC, instance_id=999_001, confirm=True)
    index = read_index(data_dir / "bundle")
    assert plan["written"] is True
    assert plan["copies_left"] == 1
    assert plan.get("off_the_shelf") is None
    assert PICTURE_DISC in [record["id"] for record in index["records"]]


def test_an_instance_that_is_not_there_is_refused(
    data_dir: Path, without_folsom: FakeWriteDiscogs
) -> None:
    plan = _remove(data_dir, without_folsom, PICTURE_DISC, instance_id=123, confirm=True)
    assert "instance 123" in plan["refused"]
    assert without_folsom.removed == []


def _lend(data_dir: Path, release_id: int, person: str, **kwargs: Any) -> dict[str, Any]:
    conn = db.connect(data_dir / "vinyl.db")
    try:
        return intake.lend_record(
            conn,
            release_id=release_id,
            person=person,
            config=ShelfConfig(),
            bundle_dir=data_dir / "bundle",
            **kwargs,
        )
    finally:
        conn.close()


def _return(data_dir: Path, release_id: int) -> dict[str, Any]:
    conn = db.connect(data_dir / "vinyl.db")
    try:
        return intake.return_record(
            conn,
            release_id=release_id,
            config=ShelfConfig(),
            bundle_dir=data_dir / "bundle",
        )
    finally:
        conn.close()


def test_a_lent_record_stays_in_the_collection(
    data_dir: Path, without_folsom: FakeWriteDiscogs
) -> None:
    before = read_index(data_dir / "bundle")["count"]
    result = _lend(data_dir, PICTURE_DISC, "a neighbour", note="borrowed at the barbecue")
    index = read_index(data_dir / "bundle")
    record = next(r for r in index["records"] if r["id"] == PICTURE_DISC)
    assert result["lent"] is True
    assert index["count"] == before
    assert record["lent"]["to"] == "a neighbour"
    assert record["lent"]["note"] == "borrowed at the barbecue"
    assert without_folsom.removed == []


def test_a_lent_record_says_who_has_it_in_the_detail_file(data_dir: Path, without_folsom) -> None:
    _lend(data_dir, PICTURE_DISC, "a neighbour")
    detail = json.loads(
        (data_dir / "bundle" / "detail" / f"{PICTURE_DISC}.json").read_text(encoding="utf-8")
    )
    assert detail["lent"]["to"] == "a neighbour"


def test_lending_a_record_the_house_does_not_own_says_so(data_dir: Path, without_folsom) -> None:
    result = _lend(data_dir, _folsom_id(), "a neighbour")
    assert result["lent"] is False
    assert "is in the collection" in result["error"]


def test_a_returned_record_is_back_on_the_shelf(data_dir: Path, without_folsom) -> None:
    _lend(data_dir, PICTURE_DISC, "a neighbour")
    result = _return(data_dir, PICTURE_DISC)
    index = read_index(data_dir / "bundle")
    record = next(r for r in index["records"] if r["id"] == PICTURE_DISC)
    assert result["returned"] is True
    assert result["was_with"] == "a neighbour"
    assert "lent" not in record


def test_returning_a_record_that_is_not_out_says_so(data_dir: Path, without_folsom) -> None:
    result = _return(data_dir, PICTURE_DISC)
    assert result["returned"] is False
    assert "not out with anybody" in result["error"]


def test_removing_a_record_forgets_the_loan(data_dir: Path, without_folsom) -> None:
    _lend(data_dir, PICTURE_DISC, "a neighbour")
    _remove(data_dir, without_folsom, PICTURE_DISC, confirm=True)
    conn = db.connect(data_dir / "vinyl.db")
    try:
        assert db.get_loan(conn, PICTURE_DISC) is None
    finally:
        conn.close()
