"""``/`` serves the built frontend, with SPA fallback, when it is present."""

from __future__ import annotations

import httpx
from joshua_tasks import static


def test_build_static_app_returns_none_when_the_directory_is_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(static.STATIC_DIR_ENV, str(tmp_path / "does-not-exist"))
    assert static.build_static_app() is None


def test_resolve_static_dir_defaults_to_the_dockerfile_path(monkeypatch) -> None:
    monkeypatch.delenv(static.STATIC_DIR_ENV, raising=False)
    assert str(static.resolve_static_dir()) == static.DEFAULT_STATIC_DIR


async def test_root_serves_index_html_with_spa_fallback(app_factory, monkeypatch, tmp_path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Tasks</title>")
    monkeypatch.setenv(static.STATIC_DIR_ENV, str(dist))

    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        root = await client.get("/")
        deep_link = await client.get("/some/unknown/route")
        health = await client.get("/healthz")

    assert root.status_code == 200 and "Tasks" in root.text
    assert deep_link.status_code == 200 and "Tasks" in deep_link.text
    assert health.json() == {"ok": True}
