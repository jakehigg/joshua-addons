"""The sync, end to end against the captured fixtures, with fake clients."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from conftest import FakeDiscogs, FakeMusicBrainz, collection_items, load_fixture, sync_into
from joshua_vinyl import db, sync
from joshua_vinyl.config import Overrides, Section, ShelfConfig

COLOR_BEFORE_THE_SUN = 7590859
DYLAN = 7823049
BEATLES = 33300852
NUGGETS = 2025582
MORRICONE = 1853642
GOLDBERG = 11059794
CASH = 24194891


def _index(data_dir: Path) -> dict:
    return json.loads((data_dir / "bundle" / "index.json").read_text(encoding="utf-8"))


def _by_id(index: dict) -> dict[int, dict]:
    return {record["id"]: record for record in index["records"]}


def test_normalize_a_collection_item() -> None:
    item = load_fixture("discogs_collection_page1.json")["releases"][0]
    fields = sync.normalize_item(item)
    assert fields["discogs_release_id"] == COLOR_BEFORE_THE_SUN
    assert fields["artist"] == "Coheed And Cambria"
    assert fields["primary_artist"] == "Coheed And Cambria"
    assert fields["title"] == "The Color Before The Sun"
    assert fields["year"] == 2015
    assert fields["label"] == "300 Entertainment"
    assert fields["catalog_no"] == "551821-1"
    assert fields["format"] == "Vinyl, LP, Album, Limited Edition, Picture Disc"
    assert fields["genres"] == ["Rock"]
    assert fields["styles"] == []
    assert fields["thumb_url"].startswith("https://i.discogs.com/")
    assert fields["cover_url"].startswith("https://i.discogs.com/")
    assert fields["added_at"] == item["date_added"]
    assert fields["instance_id"] == item["instance_id"]


def test_normalize_a_full_release_uses_images_and_treats_year_zero_as_absent() -> None:
    release = load_fixture("discogs_release_beatles_abbey_road.json")
    assert release["year"] == 0
    fields = sync.normalize_item(release)
    assert fields["year"] is None
    assert fields["thumb_url"] == release["images"][0]["uri150"]
    assert fields["cover_url"] == release["images"][0]["uri"]
    assert fields["country"] == release["country"]


def test_normalize_keeps_the_credit_line_with_join_words() -> None:
    release = load_fixture("discogs_release_gould_goldberg.json")
    fields = sync.normalize_item(release)
    assert fields["artist"] == "Bach / Glenn Gould"
    assert fields["primary_artist"] == "Johann Sebastian Bach"


def test_normalize_a_release_with_no_labels_or_formats() -> None:
    fields = sync.normalize_item({"id": 1, "title": "X", "artists": [{"name": "Y (3)"}]})
    assert fields["label"] is None
    assert fields["catalog_no"] is None
    assert fields["format"] is None
    assert fields["artist"] == "Y"
    assert fields["thumb_url"] is None


def test_a_catalog_number_of_none_is_absent() -> None:
    fields = sync.normalize_item(
        {"id": 1, "title": "X", "artists": [], "labels": [{"name": "L", "catno": "none"}]}
    )
    assert fields["label"] == "L"
    assert fields["catalog_no"] is None


def test_full_sync_files_every_record_where_the_house_rules_say(synced: Path) -> None:
    index = _index(synced)
    records = _by_id(index)
    assert index["count"] == 10
    assert records[DYLAN]["section"] == "D", "a solo artist files under surname"
    assert records[BEATLES]["section"] == "B", "a leading The is ignored"
    assert records[COLOR_BEFORE_THE_SUN]["section"] == "C"
    assert records[CASH]["section"] == "C"
    assert records[NUGGETS]["section"] == "Compilations & Soundtracks"
    assert records[MORRICONE]["section"] == "Compilations & Soundtracks", (
        "a soundtrack by one composer is never filed under the composer"
    )
    assert index["sections"] == ["B", "C", "D", "J", "Compilations & Soundtracks"]


def test_full_sync_derives_the_facets(synced: Path) -> None:
    index = _index(synced)
    records = _by_id(index)
    assert records[CASH]["facets"] == ["Country"]
    assert records[CASH]["facet"] == "Country"
    assert records[COLOR_BEFORE_THE_SUN]["facets"] == ["Rock"]
    assert "styles" not in records[COLOR_BEFORE_THE_SUN]
    names = [facet["name"] for facet in index["facets"]]
    assert names[0] == "Rock"
    assert "Country" in names
    assert "Folk, World, & Country" not in names
    rock = index["facets"][0]
    assert rock["count"] == 7
    assert rock["styles"][0] == {"name": "Prog Rock", "count": 3}


def test_full_sync_caches_art_once(
    data_dir: Path, fake_discogs: FakeDiscogs, fake_musicbrainz: FakeMusicBrainz
) -> None:
    first = sync_into(data_dir, fake_discogs, fake_musicbrainz)
    assert first.count == 10
    assert first.art_cached == 20
    assert (data_dir / "art" / f"{DYLAN}-thumb.jpg").is_file()
    assert (data_dir / "art" / f"{DYLAN}.jpg").is_file()
    records = _by_id(_index(data_dir))
    assert records[DYLAN]["thumb"] == f"art/{DYLAN}-thumb.jpg"
    assert records[DYLAN]["cover"] == f"art/{DYLAN}.jpg"

    second = sync_into(data_dir, fake_discogs, fake_musicbrainz)
    assert second.art_cached == 0
    assert len(fake_discogs.downloads) == 20


def test_full_sync_resolves_each_artist_once_and_caches_it(
    data_dir: Path, fake_discogs: FakeDiscogs, fake_musicbrainz: FakeMusicBrainz
) -> None:
    sync_into(data_dir, fake_discogs, fake_musicbrainz)
    assert fake_musicbrainz.lookups.count("Coheed And Cambria") == 1
    sync_into(data_dir, fake_discogs, fake_musicbrainz)
    assert fake_musicbrainz.lookups.count("Coheed And Cambria") == 1
    conn = db.connect(data_dir / "vinyl.db")
    cached = db.get_artist(conn, "Bob Dylan")
    assert cached is not None
    assert cached["sort_name"] == "Dylan, Bob"
    assert cached["source"] == sync.SOURCE_MUSICBRAINZ
    conn.close()


def test_an_unresolved_artist_keeps_its_name_and_is_reported(synced: Path) -> None:
    conn = db.connect(synced / "vinyl.db")
    album = db.get_album(conn, GOLDBERG)
    assert album is not None
    assert album["artist_sort"] == "Johann Sebastian Bach"
    assert album["artist_sort_source"] == sync.SOURCE_UNRESOLVED
    assert db.get_artist(conn, "Johann Sebastian Bach") is None, "tried again next sync"
    conn.close()


def test_sync_result_lists_the_unresolved_artists(
    data_dir: Path, fake_discogs: FakeDiscogs, fake_musicbrainz: FakeMusicBrainz
) -> None:
    result = sync_into(data_dir, fake_discogs, fake_musicbrainz)
    assert result.unresolved_artists == ["Bach / Glenn Gould"]
    assert result.as_dict()["count"] == 10


def test_no_musicbrainz_client_means_every_artist_is_unresolved(
    data_dir: Path, fake_discogs: FakeDiscogs
) -> None:
    result = sync_into(data_dir, fake_discogs, None)
    assert len(result.unresolved_artists) == 10
    records = _by_id(_index(data_dir))
    assert records[BEATLES]["section"] == "T", "without a sort-name, The Beatles files under T"


def test_overrides_win_over_musicbrainz_and_the_derived_rules(
    data_dir: Path, fake_discogs: FakeDiscogs, fake_musicbrainz: FakeMusicBrainz
) -> None:
    config = ShelfConfig(
        overrides=Overrides(
            artist_sort={"Johann Sebastian Bach": "Gould, Glenn"},
            section={str(NUGGETS): "N"},
            primary_facet={str(CASH): "Folk & World"},
        )
    )
    sync_into(data_dir, fake_discogs, fake_musicbrainz, config)
    records = _by_id(_index(data_dir))
    assert records[GOLDBERG]["section"] == "G"
    assert records[GOLDBERG]["artist_sort"] == "Gould, Glenn"
    assert records[NUGGETS]["section"] == "N"
    assert records[CASH]["facet"] == "Folk & World"
    assert "Johann Sebastian Bach" not in fake_musicbrainz.lookups


def test_split_special_sections_come_from_configuration(
    data_dir: Path, fake_discogs: FakeDiscogs, fake_musicbrainz: FakeMusicBrainz
) -> None:
    config = ShelfConfig(
        sections=[
            Section(name="Soundtracks", traits=["soundtrack"]),
            Section(name="Compilations", traits=["compilation"]),
        ]
    )
    sync_into(data_dir, fake_discogs, fake_musicbrainz, config)
    index = _index(data_dir)
    records = _by_id(index)
    assert records[MORRICONE]["section"] == "Soundtracks"
    assert records[NUGGETS]["section"] == "Compilations"
    assert index["sections"][-2:] == ["Soundtracks", "Compilations"]


def test_a_record_that_left_the_collection_is_removed(
    data_dir: Path, fake_musicbrainz: FakeMusicBrainz
) -> None:
    items = collection_items()
    sync_into(data_dir, FakeDiscogs(items), fake_musicbrainz)
    (data_dir / "bundle" / "detail" / f"{DYLAN}.json").is_file()
    result = sync_into(data_dir, FakeDiscogs([i for i in items if i["id"] != DYLAN]), None)
    assert result.count == 9
    assert result.removed == 1
    assert DYLAN not in _by_id(_index(data_dir))
    assert not (data_dir / "bundle" / "detail" / f"{DYLAN}.json").exists()


def test_a_failed_art_download_is_counted_and_the_record_still_lands(
    data_dir: Path, fake_musicbrainz: FakeMusicBrainz
) -> None:
    items = collection_items()[:1]
    discogs = FakeDiscogs(items)
    discogs.fail_urls.add(items[0]["basic_information"]["cover_image"])
    result = sync_into(data_dir, discogs, fake_musicbrainz)
    assert result.count == 1
    assert result.art_failed == 1
    assert result.art_cached == 1
    record = _by_id(_index(data_dir))[COLOR_BEFORE_THE_SUN]
    assert "thumb" in record
    assert "cover" not in record


def test_sync_records_the_last_run_in_meta_and_the_detail_files(synced: Path) -> None:
    conn = db.connect(synced / "vinyl.db")
    assert db.get_meta(conn, sync.META_LAST_RESULT) == "ok"
    last = db.get_meta(conn, sync.META_LAST_SYNC)
    assert last is not None
    datetime.fromisoformat(last)
    conn.close()
    detail = json.loads(
        (synced / "bundle" / "detail" / f"{MORRICONE}.json").read_text(encoding="utf-8")
    )
    assert detail["section"] == "Compilations & Soundtracks"
    assert detail["soundtrack"] is True
    assert detail["compilation"] is True
    assert detail["country"] == "Italy"


def test_a_fixed_now_stamps_both_ends_of_the_result(
    data_dir: Path, fake_discogs: FakeDiscogs, fake_musicbrainz: FakeMusicBrainz
) -> None:
    conn = db.connect(data_dir / "vinyl.db")
    when = datetime(2026, 9, 8, 3, 0, tzinfo=UTC)
    result = sync.run_sync(
        conn,
        discogs=fake_discogs,
        musicbrainz=fake_musicbrainz,
        username="example-user",
        config=ShelfConfig(),
        art_dir=data_dir / "art",
        bundle_dir=data_dir / "bundle",
        now=when,
    )
    conn.close()
    assert result.started_at == result.finished_at == "2026-09-08T03:00:00+00:00"
