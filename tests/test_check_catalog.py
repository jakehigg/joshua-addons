"""Tests for scripts/check_catalog.py: the catalog names every addon, and only those."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import check_catalog

ROOT = Path(__file__).resolve().parent.parent

GOOD = """
addons:
  - name: sample
    summary: A sample.
    image: ghcr.io/o/joshua-addons-sample
    docs: https://example.com/sample
    since: 2026.10.1
"""


def _tree(tmp_path: Path, server_name: str = "sample", with_worker: bool = False) -> Path:
    addons = tmp_path / "addons"
    pkg = addons / "sample" / "joshua_sample"
    pkg.mkdir(parents=True)
    (addons / "sample" / "Dockerfile").write_text("FROM scratch\n")
    (pkg / "server.py").write_text(f'mcp = MCPServer(name="{server_name}", version="x")\n')
    if with_worker:
        # A second image with a package that serves no MCP, as developer-worker.
        worker_pkg = addons / "sample-worker" / "joshua_sample_worker"
        worker_pkg.mkdir(parents=True)
        (addons / "sample-worker" / "Dockerfile").write_text("FROM scratch\n")
        (worker_pkg / "main.py").write_text("print('no server here')\n")
    return addons


def test_the_committed_catalog_is_clean() -> None:
    problems = check_catalog.check(check_catalog.CATALOG.read_text(), check_catalog.addon_dirs())
    assert problems == []


def test_a_clean_tree_passes(tmp_path) -> None:
    assert check_catalog.check(GOOD, check_catalog.addon_dirs(_tree(tmp_path))) == []


def test_a_second_image_that_serves_no_mcp_is_not_an_addon(tmp_path) -> None:
    dirs = check_catalog.addon_dirs(_tree(tmp_path, with_worker=True))
    assert set(dirs) == {"sample"}


def test_an_addon_with_no_entry_fails(tmp_path) -> None:
    addons = _tree(tmp_path)
    (addons / "other" / "joshua_other").mkdir(parents=True)
    (addons / "other" / "Dockerfile").write_text("FROM scratch\n")
    problems = check_catalog.check(GOOD, check_catalog.addon_dirs(addons))
    assert problems == ["other: addons/other/ has no entry in catalog.yaml"]


def test_an_entry_with_no_addon_fails(tmp_path) -> None:
    text = (
        GOOD + "  - name: ghost\n    summary: x\n    image: i\n    docs: d\n    since: 2026.10.1\n"
    )
    problems = check_catalog.check(text, check_catalog.addon_dirs(_tree(tmp_path)))
    assert any(p.startswith("ghost: no addons/ghost/") for p in problems)


def test_a_server_name_that_differs_fails_unless_declared(tmp_path) -> None:
    dirs = check_catalog.addon_dirs(_tree(tmp_path, server_name="joshua-sample"))
    assert any("'server' is 'sample'" in p for p in check_catalog.check(GOOD, dirs))
    declared = GOOD.replace("  - name: sample\n", "  - name: sample\n    server: joshua-sample\n")
    assert check_catalog.check(declared, dirs) == []


@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        ("summary", "", "'summary' must be one line"),
        ("summary", "x" * 121, "'summary' must be one line"),
        ("since", "0.1.4", "'since' is '0.1.4', not YYYY.M.N"),
        ("since", "2026.10.01", "not YYYY.M.N"),
    ],
)
def test_a_bad_field_is_named(tmp_path, field: str, value: str, fragment: str) -> None:
    text = GOOD.replace(
        {"summary": "    summary: A sample.\n", "since": "    since: 2026.10.1\n"}[field],
        f"    {field}: '{value}'\n",
    )
    problems = check_catalog.check(text, check_catalog.addon_dirs(_tree(tmp_path)))
    assert any(fragment in p for p in problems)


def test_a_bad_requires_joshua_ai_is_named(tmp_path) -> None:
    text = GOOD + "    requires_joshua_ai: latest\n"
    problems = check_catalog.check(text, check_catalog.addon_dirs(_tree(tmp_path)))
    assert any("'requires_joshua_ai' is 'latest'" in p for p in problems)


def test_the_committed_tree_runs_clean() -> None:
    assert check_catalog.main() == 0
