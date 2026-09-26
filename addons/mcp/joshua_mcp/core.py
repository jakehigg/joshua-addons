"""The call to joshua-ai core for ``semantic_search``.

Core owns Joshua's vector index and its embedding model. The addon sends a
query to core's ``POST /v1/memory/search`` with its own fleet token, and gets
passages back. Core searches the shared scope and returns only pages under
``wiki/``: wiki pages, journal pages, and profiles. It never returns a person's
own attachments.

A result path from core is relative to the data volume (``wiki/garden.md``).
This module turns it into a wiki-relative path (``garden.md``), the form every
other tool of the addon uses, and drops a path outside the wiki.
"""

from __future__ import annotations

from typing import Any

import httpx

from joshua_mcp import wiki as wikifs
from joshua_mcp.log import get_logger

logger = get_logger("joshua_mcp.core")

SEARCH_PATH = "/v1/memory/search"
TIMEOUT_S = 10.0
# The limits of core's route.
MAX_LIMIT = 25
MAX_QUERY_CHARS = 1000
SNIPPET_CHARS = 400

_WIKI_PREFIX = "wiki/"

# The kinds of core's index that each source of the addon asks for. A
# knowledge page is a wiki page to core; the source of each path is checked
# again after the answer.
KINDS: dict[str | None, list[str]] = {
    None: ["wiki", "journal", "profile"],
    wikifs.JOURNAL: ["journal"],
    "wiki": ["wiki", "profile"],
    wikifs.KNOWLEDGE: ["wiki"],
}


class CoreError(Exception):
    """Core did not answer the search. The message never holds the token."""


class CoreSearch:
    def __init__(
        self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._transport = transport

    async def search(self, query: str, source: str | None, limit: int) -> list[dict[str, Any]]:
        """The passages for ``query``, best first, at most ``limit``.

        ``source`` is None, ``wiki``, ``journal``, or ``knowledge``.
        """
        query = query.strip()
        if not query:
            return []
        if len(query) > MAX_QUERY_CHARS:
            raise CoreError(f"query is longer than {MAX_QUERY_CHARS} characters")
        limit = max(1, min(int(limit), MAX_LIMIT))
        # A source that core cannot filter on its own loses some rows to the
        # check below, so ask for all that core gives.
        ask = limit if source in (None, wikifs.JOURNAL) else MAX_LIMIT
        body = {"query": query, "limit": ask, "kinds": KINDS[source]}
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=TIMEOUT_S,
                headers={"Authorization": f"Bearer {self._token}"},
                transport=self._transport,
            ) as client:
                response = await client.post(SEARCH_PATH, json=body)
        except httpx.HTTPError as exc:
            logger.warning({"message": "core search failed", "error": type(exc).__name__})
            raise CoreError("core is not reachable") from exc
        if response.status_code in (401, 403):
            raise CoreError(
                f"core refused the search ({response.status_code}): check CORE_TOKEN, "
                "and that core has JOSHUA_TOKEN_MCP and lists mcp in "
                "memory.search.allowed_callers"
            )
        if response.status_code == 503:
            raise CoreError("core has no embedding model loaded; try again later")
        if response.status_code != 200:
            raise CoreError(f"core answered {response.status_code}")
        try:
            hits = response.json()["results"]
        except (ValueError, KeyError, TypeError) as exc:
            raise CoreError("core answered an unknown shape") from exc
        results: list[dict[str, Any]] = []
        for hit in hits:
            result = _result(hit)
            if result is None or (source is not None and result["source"] != source):
                continue
            results.append(result)
            if len(results) >= limit:
                break
        return results


def _result(hit: Any) -> dict[str, Any] | None:
    """One result in the shape of the addon, or None for a path outside the wiki."""
    if not isinstance(hit, dict):
        return None
    path = hit.get("path")
    if not isinstance(path, str) or not path.startswith(_WIKI_PREFIX):
        return None
    rel = path[len(_WIKI_PREFIX) :]
    if not rel or ".." in rel.split("/"):
        return None
    text = " ".join(str(hit.get("text") or "").split())
    if len(text) > SNIPPET_CHARS:
        text = text[:SNIPPET_CHARS].rstrip() + "…"
    return {
        "path": rel,
        "title": hit.get("title") or rel,
        "heading": hit.get("heading") or "",
        "snippet": text,
        "source": wikifs.source_of(rel),
        "date": hit.get("date"),
        "score": hit.get("score"),
    }
