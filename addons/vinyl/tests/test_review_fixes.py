"""The failures the first review found. Each one is a case CI could not see."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import FakeDiscogs, FakeMusicBrainz, collection_items, sync_into
from joshua_vinyl import db, discogs
from joshua_vinyl.bundle import read_index
from joshua_vinyl.config import ShelfConfig, settings_from_env
from joshua_vinyl.musicbrainz import MusicBrainzClient
from joshua_vinyl.sync import META_LAST_RESULT, META_LAST_SYNC
from joshua_vinyl.throttle import Throttle

DYLAN = 7823049


def test_a_failed_release_fetch_keeps_the_country(
    data_dir: Path, fake_discogs: FakeDiscogs, fake_musicbrainz: FakeMusicBrainz
) -> None:
    """The country comes from the release, not from the collection item."""
    sync_into(data_dir, fake_discogs, fake_musicbrainz)
    conn = db.connect(data_dir / "vinyl.db")
    try:
        assert db.get_album(conn, DYLAN)["country"]
    finally:
        conn.close()

    # The second sync cannot read the releases at all.
    def fails(release_id: int, *, currency: str | None = None) -> dict[str, Any]:
        raise RuntimeError("discogs timed out")

    fake_discogs.release = fails  # type: ignore[method-assign]
    sync_into(data_dir, fake_discogs, fake_musicbrainz)
    conn = db.connect(data_dir / "vinyl.db")
    try:
        assert db.get_album(conn, DYLAN)["country"]
    finally:
        conn.close()


def test_a_hand_written_note_survives_the_nightly_sync(
    data_dir: Path, fake_discogs: FakeDiscogs, fake_musicbrainz: FakeMusicBrainz
) -> None:
    sync_into(data_dir, fake_discogs, fake_musicbrainz)
    conn = db.connect(data_dir / "vinyl.db")
    try:
        conn.execute(
            "UPDATE albums SET notes = ? WHERE discogs_release_id = ?", ("a yard sale find", DYLAN)
        )
        conn.commit()
    finally:
        conn.close()
    sync_into(data_dir, fake_discogs, fake_musicbrainz)
    conn = db.connect(data_dir / "vinyl.db")
    try:
        assert db.get_album(conn, DYLAN)["notes"] == "a yard sale find"
    finally:
        conn.close()


def test_a_failed_sync_says_so_in_the_status(
    data_dir: Path, fake_discogs: FakeDiscogs, fake_musicbrainz: FakeMusicBrainz
) -> None:
    sync_into(data_dir, fake_discogs, fake_musicbrainz)

    def refused(username: str):
        raise RuntimeError("401 from discogs")
        yield  # pragma: no cover — never reached

    fake_discogs.collection = refused  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        sync_into(data_dir, fake_discogs, fake_musicbrainz)
    conn = db.connect(data_dir / "vinyl.db")
    try:
        assert db.get_meta(conn, META_LAST_RESULT).startswith("error:")
        assert "401" in db.get_meta(conn, META_LAST_RESULT)
        assert db.get_meta(conn, META_LAST_SYNC)
    finally:
        conn.close()


def test_one_release_owned_twice_is_counted_once(
    data_dir: Path, fake_musicbrainz: FakeMusicBrainz
) -> None:
    items = collection_items()
    second = dict(items[0])
    second["instance_id"] = second["instance_id"] + 1
    discogs_fake = FakeDiscogs([*items, second])
    result = sync_into(data_dir, discogs_fake, fake_musicbrainz)
    index = read_index(data_dir / "bundle")
    assert result.count == len(items)
    assert index["count"] == len(items)
    fetched = [call[0] for call in discogs_fake.release_calls]
    assert len(fetched) == len(set(fetched))


def test_a_bad_sync_time_stops_the_process_at_startup(monkeypatch) -> None:
    monkeypatch.setenv("VINYL_SYNC_TIME", "3am")
    with pytest.raises(ValueError, match="expected HH:MM"):
        settings_from_env()


def test_an_empty_sync_time_is_no_schedule(monkeypatch) -> None:
    monkeypatch.setenv("VINYL_SYNC_TIME", "")
    assert settings_from_env().sync_time is None


def test_a_musicbrainz_network_error_does_not_stop_the_sync() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = MusicBrainzClient(
        "test-agent/1.0",
        transport=httpx.MockTransport(handler),
        throttle=Throttle(0, sleep=lambda s: None),
        sleep=lambda s: None,
    )
    try:
        assert client.search_artist("Bob Dylan") is None
    finally:
        client.close()


def test_a_quote_in_a_name_does_not_break_the_query() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"artists": []})

    client = MusicBrainzClient(
        "test-agent/1.0",
        transport=httpx.MockTransport(handler),
        throttle=Throttle(0, sleep=lambda s: None),
        sleep=lambda s: None,
    )
    try:
        client.search_artist('"Weird Al" Yankovic')
    finally:
        client.close()
    query = seen[0].url.params["query"]
    assert query.startswith('artist:"')
    assert query.endswith('"')
    assert query.count('\\"') == 2


def test_an_empty_image_is_not_a_cached_cover(
    data_dir: Path, fake_discogs: FakeDiscogs, fake_musicbrainz: FakeMusicBrainz
) -> None:
    fake_discogs.download = lambda url: b""  # type: ignore[method-assign]
    result = sync_into(data_dir, fake_discogs, fake_musicbrainz)
    assert result.art_failed
    assert not list((data_dir / "art").glob("*.jpg"))
    assert not list((data_dir / "art").glob("*.tmp"))


def test_a_section_override_must_name_a_section() -> None:
    with pytest.raises(ValueError, match="names no section"):
        ShelfConfig(overrides={"section": {"123": "Sondtracks"}})


def test_a_section_override_that_names_a_section_is_accepted() -> None:
    config = ShelfConfig(overrides={"section": {"123": "Compilations & Soundtracks"}})
    assert config.overrides.section["123"] == "Compilations & Soundtracks"
    assert ShelfConfig(overrides={"section": {"123": "D"}}).overrides.section["123"] == "D"


def test_an_error_message_never_names_the_account() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    client = discogs.DiscogsClient(
        "secret-token",
        "test-agent/1.0",
        transport=httpx.MockTransport(handler),
        throttle=Throttle(0, sleep=lambda s: None),
        sleep=lambda s: None,
    )
    try:
        with pytest.raises(discogs.DiscogsError) as caught:
            list(client.collection("a-private-account"))
    finally:
        client.close()
    assert "a-private-account" not in str(caught.value)
    assert "/users/…" in str(caught.value)


def test_the_bundle_still_reads_after_every_fix(
    data_dir: Path, fake_discogs: FakeDiscogs, fake_musicbrainz: FakeMusicBrainz
) -> None:
    sync_into(data_dir, fake_discogs, fake_musicbrainz)
    index = read_index(data_dir / "bundle")
    assert index["count"] == 10
    first = index["records"][0]
    detail = json.loads(
        (data_dir / "bundle" / "detail" / f"{first['id']}.json").read_text(encoding="utf-8")
    )
    assert detail["title"]
