"""The server reports ``ADDON_VERSION`` as its version, and ``dev`` without it."""

from __future__ import annotations

import importlib

import pytest
from joshua_mcp import server


@pytest.fixture
def reload_server(monkeypatch):
    """Reload the server module with ``ADDON_VERSION`` set as given; restore after."""

    def _reload(version: str | None):
        if version is None:
            monkeypatch.delenv("ADDON_VERSION", raising=False)
        else:
            monkeypatch.setenv("ADDON_VERSION", version)
        return importlib.reload(server)

    yield _reload
    monkeypatch.delenv("ADDON_VERSION", raising=False)
    importlib.reload(server)


def test_the_server_reports_the_build_version(reload_server) -> None:
    module = reload_server("2026.10.1")
    assert module.mcp.name == "joshua-mcp"
    assert module.mcp.version == "2026.10.1"


def test_a_build_with_no_version_is_dev(reload_server) -> None:
    assert reload_server(None).mcp.version == "dev"
