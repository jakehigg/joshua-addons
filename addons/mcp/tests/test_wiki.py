"""The path rules, the refused paths, the frontmatter, and the journal reader."""

from __future__ import annotations

import os
from datetime import date

import pytest
from conftest import snapshot, write
from joshua_mcp import wiki as wikifs
from joshua_mcp.wiki import WikiError


@pytest.mark.parametrize(
    "path",
    [
        "journal/2026/09/26/2026-09-26.md",
        "journal/2026/09/26/new.md",
        "people/jake.md",
        "people/new.md",
        "joshua-docs/index.md",
        "attachments/a.md",
        "../outside.md",
        "../../etc/passwd",
        "/etc/passwd.md",
        ".git/config.md",
        ".trash/old.md",
        "notes/.hidden/x.md",
        "notes/page.txt",
        "",
    ],
)
def test_write_page_refuses_and_changes_nothing(wiki, path) -> None:
    before = snapshot(wiki.parent)
    with pytest.raises(WikiError):
        wikifs.write_page(wiki, path, "# Overwrite\n", "laptop", None)
    assert snapshot(wiki.parent) == before


def test_the_journal_refusal_names_the_journal_tool(wiki) -> None:
    with pytest.raises(WikiError, match="write_journal_entry"):
        wikifs.write_page(wiki, "journal/2026/09/26/2026-09-26.md", "x", "laptop", None)


def test_a_symlink_out_of_the_wiki_is_refused(wiki, tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, wiki / "escape")
    with pytest.raises(WikiError, match="outside the wiki"):
        wikifs.write_page(wiki, "escape/page.md", "x", "laptop", None)
    assert list(outside.iterdir()) == []


def test_a_symlinked_file_out_of_the_wiki_is_not_read_or_listed(wiki, tmp_path) -> None:
    secret = write(tmp_path / "secret.md", "sourdough secret\n")
    os.symlink(secret, wiki / "link.md")
    with pytest.raises(WikiError, match="outside the wiki"):
        wikifs.read_page(wiki, "link.md")
    assert "link.md" not in [rel for rel, _ in wikifs.iter_pages(wiki)]
    listed = wikifs.list_folder(wiki)
    assert "link.md" not in [entry["name"] for entry in listed["entries"]]


def test_a_folder_name_is_refused(wiki) -> None:
    (wiki / "folder.md").mkdir()
    with pytest.raises(WikiError, match="folder"):
        wikifs.write_page(wiki, "folder.md", "x", "laptop", None)


def test_invalid_frontmatter_is_refused(wiki) -> None:
    with pytest.raises(WikiError, match="invalid YAML"):
        wikifs.write_page(wiki, "a.md", "---\nkey: [\n---\nbody\n", "laptop", None)
    with pytest.raises(WikiError, match="mapping"):
        wikifs.write_page(wiki, "a.md", "---\n- a\n---\nbody\n", "laptop", None)
    assert not (wiki / "a.md").exists()


def test_a_horizontal_rule_is_not_frontmatter() -> None:
    assert wikifs.split_frontmatter("---\nno closing fence\n") == ({}, "---\nno closing fence\n")
    assert wikifs.split_frontmatter("---\n---\nbody") == ({}, "body")


def test_a_page_larger_than_the_limit_is_refused(wiki) -> None:
    with pytest.raises(WikiError, match="larger than"):
        wikifs.write_page(wiki, "big.md", "x" * (wikifs.MAX_BYTES + 1), "laptop", None)
    assert not (wiki / "big.md").exists()


def test_write_page_with_no_git_repository_does_not_commit(wiki) -> None:
    result = wikifs.write_page(wiki, "a.md", "x", "laptop", "a note")
    assert result["committed"] is False


@pytest.mark.parametrize("slug", ["2026-09-26", "Bad Slug", "../x", "", "-x"])
def test_write_journal_entry_refuses_a_bad_slug_and_the_day_page(wiki, slug) -> None:
    before = snapshot(wiki)
    with pytest.raises(WikiError):
        wikifs.write_journal_entry(wiki, slug, "x", "laptop", date(2026, 9, 26))
    assert snapshot(wiki) == before


def test_write_journal_entry_refuses_bad_people(wiki) -> None:
    with pytest.raises(WikiError, match="people"):
        wikifs.write_journal_entry(wiki, "x", "x", "laptop", date(2026, 9, 26), "jake")  # type: ignore[arg-type]


def test_write_journal_entry_drops_the_callers_frontmatter(wiki) -> None:
    wikifs.write_journal_entry(
        wiki, "x", "---\nsource: agent\n---\n\nBody.", "laptop", date(2026, 9, 26), ["alex"]
    )
    text = (wiki / "journal" / "2026" / "09" / "26" / "x.md").read_text()
    assert "source: agent" not in text
    assert "source: joshua-mcp" in text


def test_a_journal_folder_that_links_out_is_refused(wiki, tmp_path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    os.symlink(outside, wiki / "journal" / "2026" / "10")
    with pytest.raises(WikiError, match="outside the wiki"):
        wikifs.write_journal_entry(wiki, "x", "x", "laptop", date(2026, 10, 1))
    assert list(outside.iterdir()) == []


def test_read_journal_skips_days_with_no_folder(wiki) -> None:
    assert wikifs.read_journal(wiki, date(2026, 1, 1), days=3) == []


def test_read_journal_reads_a_page_with_broken_frontmatter(wiki) -> None:
    write(wiki / "journal" / "2026" / "09" / "24" / "odd.md", "---\nkey: [\n---\nbody\n")
    [item] = wikifs.read_journal(wiki, date(2026, 9, 24))
    assert item["frontmatter"] == {}
    assert "body" in item["body"]


def test_read_page_refuses_a_file_that_is_not_a_page(wiki) -> None:
    write(wiki / "data.csv", "a,b\n")
    with pytest.raises(WikiError, match="not a page"):
        wikifs.read_page(wiki, "data.csv")
    (wiki / "bad.md").write_bytes(b"\xff\xfe")
    with pytest.raises(WikiError, match="UTF-8"):
        wikifs.read_page(wiki, "bad.md")


def test_list_folder_shows_files_and_refuses_a_missing_folder(wiki) -> None:
    write(wiki / "recipes" / "photo.jpg", "jpeg")
    listed = wikifs.list_folder(wiki, "recipes")
    assert {"name": "photo.jpg", "path": "recipes/photo.jpg", "type": "file", "bytes": 4} in (
        listed["entries"]
    )
    with pytest.raises(WikiError, match="no such folder"):
        wikifs.list_folder(wiki, "nothing")
    journal = wikifs.list_folder(wiki, source="journal")
    assert journal["path"] == "journal"


def test_commit_message_is_one_prefixed_line() -> None:
    assert wikifs.commit_message("fix\nthe  page") == "joshua-mcp: fix the page"
    assert len(wikifs.commit_message("x" * 500)) == len("joshua-mcp: ") + 200


def test_source_of() -> None:
    assert wikifs.source_of("journal/2026/09/25/a.md") == "journal"
    assert wikifs.source_of("knowledge/a.md") == "knowledge"
    assert wikifs.source_of("people/jake.md") == "wiki"
    assert wikifs.check_source("") is None
