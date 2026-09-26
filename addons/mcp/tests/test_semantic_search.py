"""``semantic_search``: the call to core, and what the tool gives back.

A MockTransport stands in for joshua-ai core, so no network and no model are
needed. The tool is called over MCP, the way Claude Code calls it.
"""

from __future__ import annotations

import json

import httpx
import pytest
from conftest import READER, mcp_session
from joshua_mcp import server
from joshua_mcp.core import CoreError, CoreSearch

CORE_TOKEN = "core-secret"


def _hit(path: str, *, kind: str = "wiki", text: str = "Water the tomatoes.", **kw) -> dict:
    return {
        "path": path,
        "title": kw.get("title", "Garden"),
        "heading": kw.get("heading", ""),
        "text": text,
        "kind": kind,
        "score": kw.get("score", 0.8),
        "date": kw.get("date"),
    }


class FakeCore:
    def __init__(self, results: list[dict] | None = None, status: int = 200) -> None:
        self.results = results or []
        self.status = status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "no"})
        return httpx.Response(200, json={"results": self.results})

    def client(self) -> CoreSearch:
        return CoreSearch(
            "http://core:8000", CORE_TOKEN, transport=httpx.MockTransport(self.handler)
        )


def _payload(result) -> dict:
    assert result.is_error is not True, result.content
    return json.loads(result.content[0].text)


def _error(result) -> str:
    assert result.is_error is True
    return result.content[0].text


@pytest.fixture
def core(app) -> FakeCore:
    fake = FakeCore()
    server._get_state().core = fake.client()
    return fake


async def test_off_without_core_url(app) -> None:
    async with mcp_session(app) as session:
        result = await session.call_tool("semantic_search", {"query": "tomatoes"})
    assert "not configured" in _error(result)


async def test_results_are_wiki_relative(app, core) -> None:
    core.results = [
        _hit("wiki/garden.md", heading="August"),
        _hit("wiki/journal/2026/09/25/garden-work.md", kind="journal", date="2026-09-25"),
    ]
    async with mcp_session(app) as session:
        result = _payload(await session.call_tool("semantic_search", {"query": "tomatoes"}))
    assert result["results"] == [
        {
            "path": "garden.md",
            "title": "Garden",
            "heading": "August",
            "snippet": "Water the tomatoes.",
            "source": "wiki",
            "date": None,
            "score": 0.8,
        },
        {
            "path": "journal/2026/09/25/garden-work.md",
            "title": "Garden",
            "heading": "",
            "snippet": "Water the tomatoes.",
            "source": "journal",
            "date": "2026-09-25",
            "score": 0.8,
        },
    ]


async def test_the_call_carries_the_core_token_and_not_the_callers(app, core) -> None:
    async with mcp_session(app) as session:
        await session.call_tool("semantic_search", {"query": "tomatoes", "limit": 3})
    [request] = core.requests
    assert request.url.path == "/v1/memory/search"
    assert request.headers["authorization"] == f"Bearer {CORE_TOKEN}"
    assert json.loads(request.content) == {
        "query": "tomatoes",
        "limit": 3,
        "kinds": ["wiki", "journal", "profile"],
    }


async def test_a_read_only_caller_can_search(app, core) -> None:
    async with mcp_session(app, token=READER) as session:
        result = await session.call_tool("semantic_search", {"query": "tomatoes"})
    assert result.is_error is not True


async def test_no_token_is_refused_before_core_is_called(app, core) -> None:
    # The auth middleware answers 401 before the MCP session opens.
    with pytest.raises(Exception):  # noqa: B017, PT011
        async with mcp_session(app, token="wrong") as session:
            await session.call_tool("semantic_search", {"query": "tomatoes"})
    assert core.requests == []


@pytest.mark.parametrize(
    ("source", "kinds"),
    [("journal", ["journal"]), ("wiki", ["wiki", "profile"])],
)
async def test_source_picks_the_kinds(app, core, source, kinds) -> None:
    async with mcp_session(app) as session:
        await session.call_tool("semantic_search", {"query": "x", "source": source})
    assert json.loads(core.requests[0].content)["kinds"] == kinds


async def test_wiki_source_leaves_out_knowledge_pages(app, core, wiki) -> None:
    (wiki / "knowledge").mkdir()
    core.results = [_hit("wiki/knowledge/k8s.md"), _hit("wiki/garden.md")]
    async with mcp_session(app) as session:
        wiki_hits = _payload(
            await session.call_tool("semantic_search", {"query": "x", "source": "wiki"})
        )
        knowledge_hits = _payload(
            await session.call_tool("semantic_search", {"query": "x", "source": "knowledge"})
        )
    assert [hit["path"] for hit in wiki_hits["results"]] == ["garden.md"]
    assert [hit["path"] for hit in knowledge_hits["results"]] == ["knowledge/k8s.md"]


async def test_knowledge_needs_the_folder(app, core) -> None:
    async with mcp_session(app) as session:
        result = await session.call_tool("semantic_search", {"query": "x", "source": "knowledge"})
    assert "no knowledge folder" in _error(result)
    assert core.requests == []


async def test_a_path_outside_the_wiki_is_dropped(app, core) -> None:
    core.results = [
        _hit("people/alex/attachments/2026/09/scan.pdf", kind="people"),
        _hit("shared/old.md"),
        _hit("wiki/../people/alex/x.md"),
        _hit("wiki/garden.md"),
    ]
    async with mcp_session(app) as session:
        result = _payload(await session.call_tool("semantic_search", {"query": "x"}))
    assert [hit["path"] for hit in result["results"]] == ["garden.md"]


@pytest.mark.parametrize(
    ("status", "match"),
    [(401, "check CORE_TOKEN"), (403, "allowed_callers"), (503, "embedding"), (500, "500")],
)
async def test_a_core_refusal_is_a_tool_error(app, core, status, match) -> None:
    core.status = status
    async with mcp_session(app) as session:
        message = _error(await session.call_tool("semantic_search", {"query": "x"}))
    assert match in message
    assert CORE_TOKEN not in message


async def test_core_unreachable_is_a_tool_error(app) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    server._get_state().core = CoreSearch(
        "http://core:8000", CORE_TOKEN, transport=httpx.MockTransport(refuse)
    )
    async with mcp_session(app) as session:
        message = _error(await session.call_tool("semantic_search", {"query": "x"}))
    assert "not reachable" in message


async def test_long_query_and_blank_query() -> None:
    fake = FakeCore()
    client = fake.client()
    with pytest.raises(CoreError, match="longer than"):
        await client.search("x" * 2000, None, 5)
    assert await client.search("   ", None, 5) == []
    assert fake.requests == []


async def test_limit_is_clamped_and_the_snippet_is_short() -> None:
    fake = FakeCore([_hit("wiki/a.md", text="word " * 500)])
    results = await fake.client().search("x", None, 999)
    assert json.loads(fake.requests[0].content)["limit"] == 25
    assert len(results[0]["snippet"]) <= 401


async def test_an_unknown_answer_shape_is_an_error() -> None:
    def odd(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"nope": 1})

    client = CoreSearch("http://core", "t", transport=httpx.MockTransport(odd))
    with pytest.raises(CoreError, match="unknown shape"):
        await client.search("x", None, 5)
