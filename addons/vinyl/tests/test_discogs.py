"""Discogs: headers, pagination, the throttle, and the retry rules. Read-only."""

from __future__ import annotations

import copy
import json

import httpx
import pytest
from conftest import load_fixture
from joshua_vinyl import discogs
from joshua_vinyl.throttle import Throttle


def _client(handler, sleeps: list[float] | None = None) -> discogs.DiscogsClient:
    return discogs.DiscogsClient(
        "secret-token",
        "test-agent/1.0",
        transport=httpx.MockTransport(handler),
        throttle=Throttle(0, sleep=lambda s: None),
        sleep=(sleeps if sleeps is not None else []).append,
    )


def test_every_request_carries_the_user_agent_and_the_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"username": "example-user"})

    assert _client(handler).identity() == {"username": "example-user"}
    assert seen[0].headers["User-Agent"] == "test-agent/1.0"
    assert seen[0].headers["Authorization"] == "Discogs token=secret-token"
    assert seen[0].url.path == "/oauth/identity"


def test_collection_reads_every_page_of_folder_zero() -> None:
    page = load_fixture("discogs_collection_page1.json")
    first, second = copy.deepcopy(page), copy.deepcopy(page)
    first["releases"], second["releases"] = page["releases"][:2], page["releases"][2:]
    for n, data in enumerate((first, second), start=1):
        data["pagination"].update({"page": n, "pages": 2, "per_page": 2})
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        number = int(request.url.params["page"])
        return httpx.Response(200, json=first if number == 1 else second)

    items = list(_client(handler).collection("example-user", per_page=2))
    assert [item["id"] for item in items] == [item["id"] for item in page["releases"]]
    assert len(seen) == 2
    assert seen[0].url.path == "/users/example-user/collection/folders/0/releases"
    assert seen[0].url.params["per_page"] == "2"
    assert seen[1].url.params["page"] == "2"


def test_a_429_waits_for_retry_after_then_tries_again() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json=load_fixture("discogs_release_dylan_highway61.json"))

    sleeps: list[float] = []
    release = _client(handler, sleeps).release(7823049)
    assert release["title"] == "Highway 61 Revisited"
    assert sleeps == [7.0]


def test_a_5xx_backs_off_and_tries_again() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(502)
        return httpx.Response(200, json={"id": 1})

    sleeps: list[float] = []
    assert _client(handler, sleeps).release(1) == {"id": 1}
    assert sleeps == [2.0, 4.0]


def test_a_persistent_5xx_raises() -> None:
    sleeps: list[float] = []
    with pytest.raises(discogs.DiscogsError):
        _client(lambda request: httpx.Response(500), sleeps).release(1)
    assert len(sleeps) == discogs.RETRIES


def test_a_4xx_raises_without_retry() -> None:
    sleeps: list[float] = []
    with pytest.raises(discogs.DiscogsError, match="404"):
        _client(lambda request: httpx.Response(404), sleeps).release(1)
    assert sleeps == []


def test_release_asks_for_the_price_in_a_currency() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": 7823049})

    client = _client(handler)
    client.release(7823049, currency="USD")
    client.release(7823049)
    assert seen[0].url.path == "/releases/7823049"
    assert seen[0].url.params["curr_abbr"] == "USD"
    assert "curr_abbr" not in seen[1].url.params


def test_download_returns_the_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "i.discogs.com"
        return httpx.Response(200, content=b"jpeg-bytes")

    client = _client(handler)
    assert client.download("https://i.discogs.com/abc.jpeg") == b"jpeg-bytes"
    client.close()


def test_the_throttle_waits_between_requests() -> None:
    waited: list[float] = []
    clock = {"t": 0.0}
    throttle = Throttle(1.0, clock=lambda: clock["t"], sleep=waited.append)
    client = discogs.DiscogsClient(
        "t",
        "ua",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        throttle=throttle,
    )
    client.release(1)
    client.release(2)
    assert waited == [1.0]


def test_search_asks_for_releases_and_drops_an_empty_field() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": [{"id": 1}]})

    results = _client(handler).search(barcode="602567725817", catno=None, artist="")
    assert results == [{"id": 1}]
    assert seen[0].url.params["type"] == "release"
    assert seen[0].url.params["barcode"] == "602567725817"
    assert "catno" not in seen[0].url.params
    assert "artist" not in seen[0].url.params


def test_the_collection_fields_come_back_as_a_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"fields": [{"id": 3, "name": "Notes"}]})

    assert _client(handler).fields("example-user") == [{"id": 3, "name": "Notes"}]


def test_adding_a_release_posts_to_the_folder() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"instance_id": 42})

    result = _client(handler).add_release("example-user", 1, 7823049)
    assert result == {"instance_id": 42}
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/users/example-user/collection/folders/1/releases/7823049"


def test_folder_zero_cannot_take_an_add() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover — never called
        raise AssertionError("no request should reach discogs")

    with pytest.raises(discogs.DiscogsError, match="folder 0"):
        _client(handler).add_release("example-user", 0, 7823049)


def test_a_field_value_goes_in_the_body() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    _client(handler).set_field("example-user", 1, 7823049, 42, 3, "a note")
    assert seen[0].method == "POST"
    assert json.loads(seen[0].content) == {"value": "a note"}
    assert "value" not in seen[0].url.params


def test_a_failed_write_is_not_retried() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500, text="server error")

    with pytest.raises(discogs.DiscogsError):
        _client(handler).add_release("example-user", 1, 7823049)
    assert len(seen) == 1


def test_the_versions_of_one_master_come_back() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"versions": [{"id": 1}, {"id": 2}]})

    assert len(_client(handler).master_versions(3986)) == 2


def test_the_collection_folders_come_back() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"folders": [{"id": 0, "name": "All"}]})

    assert _client(handler).folders("example-user") == [{"id": 0, "name": "All"}]


def test_the_copies_of_one_release_come_back_with_their_folders() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"releases": [{"instance_id": 7, "folder_id": 2}]})

    copies = _client(handler).instances("example-user", 7823049)
    assert copies == [{"instance_id": 7, "folder_id": 2}]
    assert seen[0].url.path == "/users/example-user/collection/releases/7823049"


def test_removing_an_instance_deletes_the_right_path() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    _client(handler).remove_instance("example-user", 2, 7823049, 7)
    assert seen[0].method == "DELETE"
    assert seen[0].url.path == (
        "/users/example-user/collection/folders/2/releases/7823049/instances/7"
    )


def test_a_failed_removal_is_not_retried() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500)

    with pytest.raises(discogs.DiscogsError):
        _client(handler).remove_instance("example-user", 1, 7823049, 7)
    assert len(seen) == 1
