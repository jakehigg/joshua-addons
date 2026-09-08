"""The commands: ``sync`` with real clients over a mock transport, and the usage error."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from conftest import load_fixture, release_fixtures
from joshua_vinyl import cli, config
from joshua_vinyl.discogs import DiscogsClient
from joshua_vinyl.musicbrainz import MusicBrainzClient
from joshua_vinyl.throttle import Throttle


def _discogs_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/oauth/identity":
        return httpx.Response(200, json={"username": "example-user"})
    if path == "/users/example-user/collection/folders/0/releases":
        return httpx.Response(200, json=load_fixture("discogs_collection_page1.json"))
    if request.url.host == "i.discogs.com":
        return httpx.Response(200, content=b"\xff\xd8jpeg")
    if path.startswith("/releases/"):
        release = release_fixtures().get(int(path.rsplit("/", 1)[1]))
        return httpx.Response(200, json=release) if release else httpx.Response(404)
    return httpx.Response(404)


def _musicbrainz_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=load_fixture("musicbrainz_artist_coheed_and_cambria.json"))


def _patch_clients(monkeypatch) -> None:
    def discogs(token: str, user_agent: str) -> DiscogsClient:
        return DiscogsClient(
            token,
            user_agent,
            transport=httpx.MockTransport(_discogs_handler),
            throttle=Throttle(0, sleep=lambda s: None),
        )

    def musicbrainz(user_agent: str) -> MusicBrainzClient:
        return MusicBrainzClient(
            user_agent,
            transport=httpx.MockTransport(_musicbrainz_handler),
            throttle=Throttle(0, sleep=lambda s: None),
        )

    monkeypatch.setattr(cli, "DiscogsClient", discogs)
    monkeypatch.setattr(cli, "MusicBrainzClient", musicbrainz)


def test_sync_command_learns_the_username_from_the_token(monkeypatch, tmp_path: Path) -> None:
    _patch_clients(monkeypatch)
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(config.TOKEN_ENV, "secret")
    monkeypatch.delenv(config.USERNAME_ENV, raising=False)
    assert cli.main(["sync"]) == 0
    index = json.loads((tmp_path / "bundle" / "index.json").read_text(encoding="utf-8"))
    assert index["count"] == 4
    assert {record["section"] for record in index["records"]} == {"C"}
    assert (tmp_path / "art" / "7590859-thumb.jpg").is_file()
    detail = json.loads(
        (tmp_path / "bundle" / "detail" / "7590859.json").read_text(encoding="utf-8")
    )
    assert len(detail["tracks"]) == 10
    assert detail["price"]["currency"] == "USD"


def test_sync_command_uses_a_configured_username(monkeypatch, tmp_path: Path) -> None:
    _patch_clients(monkeypatch)
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(config.TOKEN_ENV, "secret")
    monkeypatch.setenv(config.USERNAME_ENV, "example-user")
    result = cli.sync_once(config.settings_from_env())
    assert result.count == 4


def test_sync_command_fails_cleanly_without_a_token(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.delenv(config.TOKEN_ENV, raising=False)
    assert cli.main(["sync"]) == 1
    assert not (tmp_path / "bundle").exists()


def test_an_unknown_command_prints_usage(capsys) -> None:
    assert cli.main(["bogus"]) == 2
    assert cli.USAGE in capsys.readouterr().err
