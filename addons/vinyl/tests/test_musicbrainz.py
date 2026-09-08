"""MusicBrainz: the sort-name comes from the API, and a weak match is refused."""

from __future__ import annotations

import httpx
import pytest
from conftest import load_fixture
from joshua_vinyl import musicbrainz
from joshua_vinyl.throttle import Throttle


@pytest.mark.parametrize(
    ("fixture", "name", "sort_name", "type_"),
    [
        ("musicbrainz_artist_bob_dylan.json", "Bob Dylan", "Dylan, Bob", "Person"),
        ("musicbrainz_artist_the_beatles.json", "The Beatles", "Beatles, The", "Group"),
        (
            "musicbrainz_artist_ennio_morricone.json",
            "Ennio Morricone",
            "Morricone, Ennio",
            "Person",
        ),
        ("musicbrainz_artist_glenn_gould.json", "Glenn Gould", "Gould, Glenn", "Person"),
        ("musicbrainz_artist_johnny_cash.json", "Johnny Cash", "Cash, Johnny", "Person"),
        ("musicbrainz_artist_various.json", "Various", "Various Artists", "Other"),
        (
            "musicbrainz_artist_coheed_and_cambria.json",
            "Coheed And Cambria",
            "Coheed and Cambria",
            "Group",
        ),
    ],
)
def test_best_match_from_real_responses(
    fixture: str, name: str, sort_name: str, type_: str
) -> None:
    match = musicbrainz.best_match(name, load_fixture(fixture)["artists"])
    assert match is not None
    assert match.sort_name == sort_name
    assert match.type == type_
    assert match.score >= musicbrainz.MIN_SCORE


def test_a_low_score_is_no_match() -> None:
    artists = load_fixture("musicbrainz_artist_bob_dylan.json")["artists"]
    weak = [dict(artist, score=40) for artist in artists]
    assert musicbrainz.best_match("Bob Dylan", weak) is None


def test_a_high_score_with_a_different_name_is_no_match() -> None:
    artists = load_fixture("musicbrainz_artist_bob_dylan.json")["artists"]
    assert musicbrainz.best_match("Bob Marley", artists) is None


def test_an_alias_can_match() -> None:
    artists = [
        {
            "id": "x",
            "score": 100,
            "name": "Prince",
            "sort-name": "Prince",
            "type": "Person",
            "aliases": [{"name": "The Artist Formerly Known As Prince"}],
        }
    ]
    match = musicbrainz.best_match("Artist Formerly Known As Prince", artists)
    assert match is not None
    assert match.mbid == "x"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("The Beatles", "beatles"),
        ("Beatles, The", "beatles the"),
        ("Sly & The Family Stone", "sly and the family stone"),
        ("Coheed And Cambria", "coheed and cambria"),
        ("Édith Piaf", "edith piaf"),
        ("  Bob   Dylan ", "bob dylan"),
    ],
)
def test_normalize_name(raw: str, expected: str) -> None:
    assert musicbrainz.normalize_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Nirvana (2)", "Nirvana"), ("Bob Dylan", "Bob Dylan"), ("Blur (12) ", "Blur")],
)
def test_strip_discogs_suffix(raw: str, expected: str) -> None:
    assert musicbrainz.strip_discogs_suffix(raw) == expected


def _client(handler, sleeps: list[float]) -> musicbrainz.MusicBrainzClient:
    return musicbrainz.MusicBrainzClient(
        "test-agent/1.0",
        transport=httpx.MockTransport(handler),
        throttle=Throttle(0, sleep=lambda s: None),
        sleep=sleeps.append,
    )


def test_search_sends_a_user_agent_and_a_quoted_query() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=load_fixture("musicbrainz_artist_bob_dylan.json"))

    sleeps: list[float] = []
    match = _client(handler, sleeps).search_artist("Bob Dylan (2)")
    assert match is not None
    assert match.sort_name == "Dylan, Bob"
    assert seen[0].headers["User-Agent"] == "test-agent/1.0"
    assert seen[0].url.params["query"] == 'artist:"Bob Dylan"'
    assert seen[0].url.params["fmt"] == "json"
    assert sleeps == []


def test_a_503_waits_and_tries_again() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json=load_fixture("musicbrainz_artist_bob_dylan.json"))

    sleeps: list[float] = []
    match = _client(handler, sleeps).search_artist("Bob Dylan")
    assert match is not None
    assert calls["n"] == 2
    assert sleeps == [3.0]


def test_a_persistent_503_gives_up_with_none() -> None:
    sleeps: list[float] = []
    client = _client(lambda request: httpx.Response(503), sleeps)
    assert client.search_artist("Bob Dylan") is None
    assert len(sleeps) == musicbrainz.RETRIES


def test_any_other_error_is_none_not_an_exception() -> None:
    sleeps: list[float] = []
    client = _client(lambda request: httpx.Response(400, text="bad"), sleeps)
    assert client.search_artist("Bob Dylan") is None
    client.close()
