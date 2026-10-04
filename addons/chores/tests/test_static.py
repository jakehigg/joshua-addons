"""``/`` serves the built frontend, with SPA fallback, when it is present.

``/mcp`` and ``/healthz`` match before the catch-all static mount (see
``server.build_app``).
"""

from __future__ import annotations

import httpx
import pytest
from joshua_chores import static
from starlette.exceptions import HTTPException


def test_build_static_app_returns_none_when_the_directory_is_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(static.STATIC_DIR_ENV, str(tmp_path / "does-not-exist"))
    assert static.build_static_app() is None


def test_resolve_static_dir_defaults_to_the_dockerfile_path(monkeypatch) -> None:
    monkeypatch.delenv(static.STATIC_DIR_ENV, raising=False)
    assert str(static.resolve_static_dir()) == static.DEFAULT_STATIC_DIR


def _write_fixture_dist(tmp_path) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><title>Chores</title>")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log('chores');")


async def test_root_serves_index_html_and_falls_back_for_a_deep_link(
    app_factory, monkeypatch, tmp_path
) -> None:
    _write_fixture_dist(tmp_path)
    monkeypatch.setenv(static.STATIC_DIR_ENV, str(tmp_path))

    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        root = await client.get("/")
        asset = await client.get("/assets/app.js")
        deep_link = await client.get("/some-member")
        healthz = await client.get("/healthz")

    assert root.status_code == 200
    assert "Chores" in root.text
    assert asset.status_code == 200
    assert "chores" in asset.text
    assert deep_link.status_code == 200
    assert "Chores" in deep_link.text
    assert healthz.json() == {"ok": True}


async def test_static_mount_raises_a_non_404_error_again(monkeypatch, tmp_path) -> None:
    _write_fixture_dist(tmp_path)
    monkeypatch.setenv(static.STATIC_DIR_ENV, str(tmp_path))
    app = static.build_static_app()
    assert app is not None
    scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    with pytest.raises(HTTPException) as caught:
        await app.get_response("index.html", scope)
    assert caught.value.status_code == 405


async def test_root_is_404_when_dist_is_missing(app_factory, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(static.STATIC_DIR_ENV, str(tmp_path / "missing"))
    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/")
    assert response.status_code == 404
