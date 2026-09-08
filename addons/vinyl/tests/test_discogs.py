"""Discogs: headers, pagination, the throttle, and the retry rules. Read-only."""

from __future__ import annotations

import copy

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
