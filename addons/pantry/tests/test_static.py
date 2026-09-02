"""``/`` serves the built frontend, with SPA fallback, when it is present.

``/mcp``, ``/healthz``, and ``/api`` keep taking priority over the catch-all
static mount (see ``server.build_app``).
"""

from __future__ import annotations

import httpx
from joshua_pantry import static


def test_build_static_app_returns_none_when_the_directory_is_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(static.STATIC_DIR_ENV, str(tmp_path / "does-not-exist"))
    assert static.build_static_app() is None


def test_resolve_static_dir_defaults_to_the_dockerfile_path(monkeypatch) -> None:
    monkeypatch.delenv(static.STATIC_DIR_ENV, raising=False)
    assert str(static.resolve_static_dir()) == static.DEFAULT_STATIC_DIR


def _write_fixture_dist(tmp_path) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><title>Pantry</title>")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log('pantry');")


async def test_root_serves_index_html_when_dist_exists(
    app_factory, bound_db, monkeypatch, tmp_path
) -> None:
    _write_fixture_dist(tmp_path)
    monkeypatch.setenv(static.STATIC_DIR_ENV, str(tmp_path))

    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        root = await client.get("/")
        asset = await client.get("/assets/app.js")
        deep_link = await client.get("/some/unknown/route")

    assert root.status_code == 200
    assert "Pantry" in root.text

    assert asset.status_code == 200
    assert "pantry" in asset.text

    # SPA fallback: an unmatched path still serves index.html, not a 404.
    assert deep_link.status_code == 200
    assert "Pantry" in deep_link.text


async def test_mcp_healthz_and_api_take_priority_over_the_static_mount(
    app_factory, bound_db, monkeypatch, tmp_path
) -> None:
    _write_fixture_dist(tmp_path)
    monkeypatch.setenv(static.STATIC_DIR_ENV, str(tmp_path))

    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        healthz = await client.get("/healthz")
        inventory = await client.get("/api/inventory")

    assert healthz.status_code == 200
    assert healthz.json() == {"ok": True}

    assert inventory.status_code == 200
    assert inventory.json() == []


async def test_root_omitted_when_dist_is_missing(app_factory, bound_db) -> None:
    """No ``PANTRY_STATIC_DIR`` (or it points nowhere): ``/`` 404s instead of crashing."""
    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/")
    assert response.status_code == 404
