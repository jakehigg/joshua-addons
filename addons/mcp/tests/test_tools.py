"""The tools, called over MCP the way Claude Code calls them."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from conftest import mcp_session, write


def payload(result) -> dict:
    assert result.is_error is not True, result.content
    [block] = result.content
    return json.loads(block.text)


def error(result) -> str:
    assert result.is_error is True
    return result.content[0].text


def frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---\n")[1])


async def test_the_tool_list_is_final(app) -> None:
    async with mcp_session(app) as session:
        tools = await session.list_tools()
    assert sorted(tool.name for tool in tools.tools) == [
        "knowledge_read",
        "knowledge_search",
        "list",
        "read_journal",
        "read_page",
        "search",
        "semantic_search",
        "write_journal_entry",
        "write_page",
    ]


async def test_search_finds_pages_across_sources(app) -> None:
    async with mcp_session(app) as session:
        result = payload(await session.call_tool("search", {"query": "sourdough"}))
    paths = [hit["path"] for hit in result["results"]]
    assert set(paths) == {"recipes/bread.md", "journal/2026/09/25/2026-09-25.md"}
    journal = next(hit for hit in result["results"] if hit["source"] == "journal")
    assert journal["date"] == "2026-09-25"


async def test_search_by_source(app) -> None:
    async with mcp_session(app) as session:
        result = payload(
            await session.call_tool("search", {"query": "sourdough", "source": "wiki"})
        )
        bad = await session.call_tool("search", {"query": "x", "source": "memos"})
    assert [hit["path"] for hit in result["results"]] == ["recipes/bread.md"]
    assert "source must be one of" in error(bad)


async def test_read_page(app) -> None:
    async with mcp_session(app) as session:
        page = payload(await session.call_tool("read_page", {"path": "recipes/bread.md"}))
        missing = await session.call_tool("read_page", {"path": "recipes/cake.md"})
    assert page["frontmatter"] == {"title": "Sourdough bread"}
    assert page["body"].startswith("Feed the starter")
    assert "no such page" in error(missing)


async def test_list_the_top_of_the_wiki(app) -> None:
    async with mcp_session(app) as session:
        top = payload(await session.call_tool("list", {}))
        pages = payload(await session.call_tool("list", {"source": "wiki"}))
        folder = payload(await session.call_tool("list", {"path": "recipes"}))
    names = [entry["name"] for entry in top["entries"]]
    assert names == ["Home.md", "garden.md", "joshua-docs", "journal", "people", "recipes"]
    assert "journal" not in [entry["name"] for entry in pages["entries"]]
    assert folder == {
        "path": "recipes",
        "entries": [{"name": "bread.md", "path": "recipes/bread.md", "type": "page", "bytes": 82}],
    }


async def test_write_page_stamps_provenance_and_the_caller(app, wiki) -> None:
    markdown = "---\ntitle: Laptop notes\ntags: [a]\n---\n\n# Notes\n\nFrom the laptop.\n"
    async with mcp_session(app) as session:
        result = payload(
            await session.call_tool("write_page", {"path": "notes/laptop.md", "markdown": markdown})
        )
        again = payload(
            await session.call_tool("write_page", {"path": "notes/laptop.md", "markdown": "x"})
        )
    assert result["path"] == "notes/laptop.md"
    assert result["overwritten"] is False
    assert again["overwritten"] is True
    assert frontmatter(wiki / "notes" / "laptop.md") == {
        "source": "joshua-mcp",
        "author": "laptop",
    }


async def test_write_page_keeps_the_callers_frontmatter(app, wiki) -> None:
    markdown = "---\ntitle: Laptop notes\nsource: agent\n---\n\nBody.\n"
    async with mcp_session(app) as session:
        payload(await session.call_tool("write_page", {"path": "notes/b.md", "markdown": markdown}))
    assert frontmatter(wiki / "notes" / "b.md") == {
        "title": "Laptop notes",
        "source": "joshua-mcp",
        "author": "laptop",
    }
    assert (wiki / "notes" / "b.md").read_text().endswith("\n\nBody.\n")


async def test_a_write_is_found_by_the_next_search(app) -> None:
    async with mcp_session(app) as session:
        await session.call_tool("search", {"query": "garden"})
        payload(
            await session.call_tool(
                "write_page", {"path": "zebra.md", "markdown": "# Zebra\n\nStripes.\n"}
            )
        )
        result = payload(await session.call_tool("search", {"query": "stripes"}))
    assert [hit["path"] for hit in result["results"]] == ["zebra.md"]


async def test_write_journal_entry(app, wiki) -> None:
    async with mcp_session(app) as session:
        result = payload(
            await session.call_tool(
                "write_journal_entry",
                {"slug": "laptop-note", "markdown": "Jake fixed the CI.", "people": ["jake"]},
            )
        )
    # 15:00 UTC on 2026-09-26 is 11:00 in New York, the same day.
    assert result["path"] == "journal/2026/09/26/laptop-note.md"
    entry = wiki / "journal" / "2026" / "09" / "26" / "laptop-note.md"
    assert frontmatter(entry) == {
        "date": "2026-09-26",
        "people": ["jake"],
        "source": "joshua-mcp",
        "author": "laptop",
    }
    assert entry.read_text().endswith("\n\nJake fixed the CI.")


async def test_write_journal_entry_takes_a_date(app, wiki) -> None:
    async with mcp_session(app) as session:
        result = payload(
            await session.call_tool(
                "write_journal_entry",
                {"slug": "late", "markdown": "x", "date": "2026-09-20"},
            )
        )
        bad = await session.call_tool(
            "write_journal_entry", {"slug": "late", "markdown": "x", "date": "Sept 20"}
        )
    assert result["path"] == "journal/2026/09/20/late.md"
    assert "YYYY-MM-DD" in error(bad)


async def test_read_journal_yesterday(app) -> None:
    async with mcp_session(app) as session:
        result = payload(await session.call_tool("read_journal", {"date": "2026-09-25"}))
    items = result["items"]
    assert [item["kind"] for item in items] == ["day", "entry", "entry"]
    assert items[0]["path"] == "journal/2026/09/25/2026-09-25.md"
    assert items[1]["frontmatter"]["people"] == ["alex"]


async def test_read_journal_a_range_and_people(app) -> None:
    async with mcp_session(app) as session:
        result = payload(await session.call_tool("read_journal", {"days": 2, "people": ["alex"]}))
        too_many = await session.call_tool("read_journal", {"days": 400})
    paths = [item["path"] for item in result["items"]]
    assert paths == [
        "journal/2026/09/25/2026-09-25.md",
        "journal/2026/09/25/alex-visit.md",
    ]
    assert "days must be" in error(too_many)


async def test_knowledge_tools_without_the_folder(app) -> None:
    async with mcp_session(app) as session:
        found = await session.call_tool("knowledge_search", {"query": "x"})
        read = await session.call_tool("knowledge_read", {"path": "a.md"})
        listed = await session.call_tool("list", {"source": "knowledge"})
    assert error(found).endswith("no knowledge folder")
    assert error(read).endswith("no knowledge folder")
    assert error(listed).endswith("no knowledge folder")


async def test_knowledge_tools_with_the_folder(app, wiki) -> None:
    write(wiki / "knowledge" / "tides.md", "# Tides\n\nThe bay has two tides a day.\n")
    async with mcp_session(app) as session:
        found = payload(await session.call_tool("knowledge_search", {"query": "tides"}))
        page = payload(await session.call_tool("knowledge_read", {"path": "tides.md"}))
        escape = await session.call_tool("knowledge_read", {"path": "../garden.md"})
    assert [hit["path"] for hit in found["results"]] == ["knowledge/tides.md"]
    assert page["path"] == "knowledge/tides.md"
    assert is_refused(error(escape))


def is_refused(message: str) -> bool:
    return "outside" in message
