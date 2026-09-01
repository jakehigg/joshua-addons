"""Tests for scripts/list_addons.py: the addon discovery every CI matrix reads."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts import list_addons

ROOT = Path(__file__).resolve().parent.parent


def test_list_addons_finds_every_addon_with_a_dockerfile() -> None:
    names = list_addons.list_addons()
    assert names == sorted(names)
    assert "hello" in names
    for name in names:
        assert (ROOT / "addons" / name / "Dockerfile").is_file()


def test_a_directory_with_no_dockerfile_is_not_an_addon(tmp_path, monkeypatch) -> None:
    addons_dir = tmp_path / "addons"
    (addons_dir / "real").mkdir(parents=True)
    (addons_dir / "real" / "Dockerfile").write_text("FROM scratch\n")
    (addons_dir / "no-dockerfile").mkdir()
    monkeypatch.setattr(list_addons, "ADDONS_DIR", addons_dir)
    assert list_addons.list_addons() == ["real"]


def test_no_addons_directory_gives_an_empty_list(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(list_addons, "ADDONS_DIR", tmp_path / "does-not-exist")
    assert list_addons.list_addons() == []


def test_cli_prints_a_compact_json_array_and_takes_no_argument() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "list_addons.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == list_addons.list_addons()
    assert "\n" not in result.stdout.strip()
