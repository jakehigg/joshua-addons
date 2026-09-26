"""A write commits to the wiki repository through ``joshua_shared.wikigit``.

Needs the ``git`` binary, so it is an integration test.
"""

from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

import pytest
from joshua_mcp import wiki as wikifs
from joshua_shared import wikigit

pytestmark = pytest.mark.integration


def git(wiki: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=wiki, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(wiki: Path) -> Path:
    assert wikigit.ensure_repo(wiki) is True
    assert wikigit.commit(wiki, None, "initial") is True
    return wiki


def test_write_page_commits_with_the_addon_message(repo: Path) -> None:
    result = wikifs.write_page(repo, "notes/a.md", "# A\n", "laptop", "add the A notes")
    assert result["committed"] is True
    assert git(repo, "log", "-1", "--format=%s").strip() == "joshua-mcp: add the A notes"
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["notes/a.md"]


def test_write_page_has_a_default_message(repo: Path) -> None:
    wikifs.write_page(repo, "b.md", "# B\n", "laptop", None)
    assert git(repo, "log", "-1", "--format=%s").strip() == "joshua-mcp: write_page b.md"


def test_write_journal_entry_commits_only_the_entry(repo: Path) -> None:
    (repo / "stray.md").write_text("the nightly commits this, not the addon\n")
    result = wikifs.write_journal_entry(repo, "note", "x", "laptop", date(2026, 9, 26))
    assert result["committed"] is True
    subject = git(repo, "log", "-1", "--format=%s").strip()
    assert subject == "joshua-mcp: write_journal_entry journal/2026/09/26/note.md"
    assert git(repo, "status", "--porcelain").strip() == "?? stray.md"
