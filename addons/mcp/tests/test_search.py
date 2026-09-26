"""The BM25 index: ranking, sources, snippets, and the refresh."""

from __future__ import annotations

import pytest
from conftest import write
from joshua_mcp.search import Index, tokenize
from joshua_mcp.wiki import WikiError


def test_tokenize() -> None:
    assert tokenize("Sourdough, BREAD & crème!") == ["sourdough", "bread", "crème"]


def test_a_page_that_repeats_a_word_ranks_first(wiki) -> None:
    write(wiki / "water.md", "# Water\n\nwater water water, and more water.\n")
    index = Index(wiki)
    results = index.search("water")
    assert results[0]["path"] == "water.md"
    assert {hit["path"] for hit in results} >= {"garden.md", "journal/2026/09/25/garden-work.md"}


def test_hidden_and_attachment_folders_are_not_indexed(wiki) -> None:
    index = Index(wiki)
    index.search("sourdough")
    assert ".trash/old.md" not in [hit["path"] for hit in index.search("trash")]
    assert index.search("binary") == []


def test_the_title_comes_from_frontmatter_a_heading_or_the_name(wiki) -> None:
    write(wiki / "plain.md", "no heading, only zanzibar\n")
    index = Index(wiki)
    assert index.search("sourdough", "wiki")[0]["title"] == "Sourdough bread"
    assert index.search("tomatoes")[0]["title"] == "Garden"
    assert index.search("zanzibar")[0]["title"] == "plain"


def test_the_snippet_is_the_matching_line(wiki) -> None:
    [hit] = Index(wiki).search("tomatoes")
    assert hit["snippet"] == "The tomatoes need water every day in August."


def test_a_title_match_gets_the_first_line_as_the_snippet(wiki) -> None:
    write(wiki / "zebra.md", "---\ntitle: Zebra\n---\n\nStripes only.\n")
    [hit] = Index(wiki).search("zebra")
    assert hit["snippet"] == "Stripes only."


def test_the_index_follows_changes_and_deletes(wiki) -> None:
    index = Index(wiki)
    assert index.search("quokka") == []
    page = write(wiki / "animals.md", "a quokka\n")
    assert [hit["path"] for hit in index.search("quokka")] == ["animals.md"]
    page.write_text("a wombat, a longer page than before\n")
    assert index.search("quokka") == []
    page.unlink()
    assert index.search("wombat") == []
    assert "animals.md" not in [hit["path"] for hit in index.search("page")]


def test_an_unreadable_page_is_dropped(wiki) -> None:
    index = Index(wiki)
    index.search("garden")
    count = len(index)
    (wiki / "garden.md").write_bytes(b"\xff\xfe garden garden")
    index.search("garden")
    assert len(index) == count - 1


def test_limit_and_empty_query(wiki) -> None:
    index = Index(wiki)
    assert len(index.search("the", limit=1)) == 1
    with pytest.raises(WikiError, match="no words"):
        index.search("  !! ")


def test_a_missing_wiki_finds_nothing(tmp_path) -> None:
    assert Index(tmp_path / "nothing").search("x") == []
