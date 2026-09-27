"""The joshua-mcp addon: Joshua's wiki, journal, and knowledge folder over MCP.

The addon serves MCP at ``/mcp`` over streamable HTTP. ``semantic_search`` asks
joshua-ai core to search by meaning, when ``CORE_URL`` and ``CORE_TOKEN`` are
set; every other tool reads or writes the data volume. ``GET /healthz`` stays
open and names no path and no person. When a bearer is configured
(``ADDON_TOKEN`` or ``MCP_TOKENS``), every other route needs
``Authorization: Bearer <token>``; a missing or wrong token gets 401. The
token also names the caller: a write records that name, and a read-only
caller gets a "403 forbidden" error from every write tool.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from joshua_mcp import wiki as wikifs
from joshua_mcp.config import ANONYMOUS, Caller, Settings, settings_from_env
from joshua_mcp.core import CoreError, CoreSearch
from joshua_mcp.log import get_logger
from joshua_mcp.search import Index

logger = get_logger("joshua_mcp")

OPEN_PATHS = frozenset({"/healthz"})
_BEARER = "Bearer "

mcp = MCPServer(name="joshua-mcp")


@dataclass
class _State:
    settings: Settings
    index: Index
    # None when CORE_URL is not set.
    core: CoreSearch | None = None


_state: _State | None = None


def _get_state() -> _State:
    if _state is None:
        raise RuntimeError("build_app has not run")
    return _state


def _now() -> datetime:
    """The current time. A seam the tests override for a fixed clock."""
    return datetime.now(UTC)


# --- callers -------------------------------------------------------------


def find_caller(settings: Settings, authorization: str | None) -> Caller | None:
    """The caller that ``authorization`` names, or None for a missing or wrong token.

    Every configured token is compared, with ``hmac.compare_digest``, so the
    time taken does not tell which caller a token is close to.
    """
    if settings.open:
        return Caller(name=ANONYMOUS, readonly=settings.anonymous_readonly)
    raw = authorization or ""
    provided = raw[len(_BEARER) :] if raw.startswith(_BEARER) else ""
    found = None
    for token, caller in settings.tokens.items():
        if hmac.compare_digest(provided.encode(), token.encode()) and provided:
            found = caller
    return found


def _caller(ctx: Context) -> Caller:
    # The auth middleware checked this same header on the HTTP request that
    # carries this message, so here it only selects the caller's name.
    settings = _get_state().settings
    headers = ctx.headers or {}
    caller = find_caller(settings, headers.get("authorization"))
    if caller is None:
        raise ToolError("401 unauthorized")
    return caller


def _writer(ctx: Context) -> Caller:
    caller = _caller(ctx)
    if caller.readonly:
        logger.warning(
            {"message": "refused a write from a read-only caller", "caller": caller.name}
        )
        raise ToolError(f"403 forbidden: the caller {caller.name!r} is read-only")
    return caller


async def _run(func, *args: Any) -> Any:
    """Run a blocking file or git operation off the event loop, as a tool result."""
    try:
        return await anyio.to_thread.run_sync(func, *args)
    except wikifs.WikiError as exc:
        raise ToolError(str(exc)) from exc


def _parse_day(value: str | None) -> date:
    if value:
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ToolError("date must be YYYY-MM-DD") from exc
    return _now().astimezone(_get_state().settings.timezone).date()


def _source(value: str | None) -> str | None:
    try:
        return wikifs.check_source(value)
    except wikifs.WikiError as exc:
        raise ToolError(str(exc)) from exc


# --- tools ---------------------------------------------------------------


@mcp.tool()
async def search(query: str, ctx: Context, source: str | None = None, limit: int = 10) -> dict:
    """Search the wiki, the journal, and the knowledge folder by words.

    Returns results, the best pages first.

    source is one of wiki (the pages), journal (Joshua's journal), or knowledge
    (the knowledge folder); leave it empty to search all three. Each result has
    the path, the title, a snippet, the source, and, for a journal page, the
    date. The search is lexical: it finds the words you give, not their meaning.
    """
    _caller(ctx)
    src = _source(source)
    state = _get_state()
    if src == wikifs.KNOWLEDGE:
        await _run(wikifs.require_knowledge, state.settings.wiki)
    return {"results": await _run(state.index.search, query, src, limit)}


@mcp.tool()
async def semantic_search(
    query: str, ctx: Context, source: str | None = None, limit: int = 10
) -> dict:
    """Search the wiki and the journal by meaning, with Joshua's own index.

    Returns passages, the best first. Use it for a question in your own words,
    when you do not know the words a page uses. Use search to find exact words.

    source is one of wiki (the pages and the profiles), journal (Joshua's
    journal), or knowledge (the knowledge folder); leave it empty to search
    all of them. limit is 1 to 25. Each result has the path, the title, the
    heading of the passage, a snippet, the source, the date of a journal page,
    and a score from 0 to 1. Joshua's core does the search, and it indexes a
    new page within about a minute.
    """
    _caller(ctx)
    src = _source(source)
    state = _get_state()
    if state.core is None:
        raise ToolError("semantic search is not configured: set CORE_URL and CORE_TOKEN")
    if src == wikifs.KNOWLEDGE:
        await _run(wikifs.require_knowledge, state.settings.wiki)
    try:
        return {"results": await state.core.search(query, src, limit)}
    except CoreError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
async def read_page(path: str, ctx: Context) -> dict:
    """Read one wiki page: its frontmatter and its body.

    path is relative to the wiki, such as people/alex.md or
    journal/2026/09/25/2026-09-25.md.
    """
    _caller(ctx)
    page = await _run(wikifs.read_page, _get_state().settings.wiki, path)
    return page.as_dict()


@mcp.tool(name="list")
async def list_(ctx: Context, path: str | None = None, source: str | None = None) -> dict:
    """List one folder of the wiki: its folders, its pages, and its other files.

    Give path (relative to the wiki) for one folder. Without a path, source
    selects the top folder: wiki (the pages, without the journal and the
    knowledge folder), journal, or knowledge. With neither, lists the top of
    the wiki.
    """
    _caller(ctx)
    return await _run(wikifs.list_folder, _get_state().settings.wiki, path, source)


@mcp.tool()
async def write_page(path: str, markdown: str, ctx: Context, message: str | None = None) -> dict:
    """Create or replace one wiki page, and commit it to the wiki repository.

    path is relative to the wiki and ends in .md. The journal/, people/,
    joshua-docs/, and attachments/ folders are refused: the journal has its
    own tool, and the profiles and the documentation are Joshua's. The page
    gets source: joshua-mcp and author: <caller> in its frontmatter; any
    other frontmatter you send is kept. message is the commit message.
    """
    caller = _writer(ctx)
    wiki = _get_state().settings.wiki
    return await _run(wikifs.write_page, wiki, path, markdown, caller.name, message)


@mcp.tool()
async def write_journal_entry(
    slug: str,
    markdown: str,
    ctx: Context,
    people: list[str] | None = None,
    date: str | None = None,
) -> dict:
    """Add one entry to Joshua's journal, at journal/YYYY/MM/DD/<slug>.md.

    slug is a short lowercase name, such as alex-visit. date is YYYY-MM-DD
    and defaults to today. people names the person ids the entry is about.
    An entry at the same day and slug is replaced. The day page, which the
    nightly run writes, is never written here. The entry gets date, people,
    source: joshua-mcp, and author: <caller> as its frontmatter.
    """
    caller = _writer(ctx)
    day = _parse_day(date)
    wiki = _get_state().settings.wiki
    return await _run(wikifs.write_journal_entry, wiki, slug, markdown, caller.name, day, people)


@mcp.tool()
async def read_journal(
    ctx: Context,
    date: str | None = None,
    days: int = 1,
    people: list[str] | None = None,
) -> dict:
    """Read Joshua's journal: items holds the day pages and the entries, oldest first.

    date is the last day (YYYY-MM-DD, default today). days is how many days
    to read, up to 31, ending on that date. people keeps only the entries
    that name one of those person ids; a day page is always included.
    """
    _caller(ctx)
    end = _parse_day(date)
    items = await _run(wikifs.read_journal, _get_state().settings.wiki, end, days, people)
    return {"items": items}


@mcp.tool()
async def knowledge_search(query: str, ctx: Context, limit: int = 10) -> dict:
    """Search the knowledge folder by words. The same as search with source knowledge."""
    return await search(query, ctx, source=wikifs.KNOWLEDGE, limit=limit)


@mcp.tool()
async def knowledge_read(path: str, ctx: Context) -> dict:
    """Read one page of the knowledge folder. path is relative to knowledge/."""
    _caller(ctx)
    wiki = _get_state().settings.wiki
    await _run(wikifs.require_knowledge, wiki)
    rel = f"{wikifs.KNOWLEDGE}/{path.lstrip('/')}"
    page = await _run(wikifs.read_page, wiki, rel)
    if wikifs.source_of(page.path) != wikifs.KNOWLEDGE:
        raise ToolError(f"path is outside the knowledge folder: {path}")
    return page.as_dict()


# --- HTTP ----------------------------------------------------------------


class BearerAuthMiddleware:
    """Require a known bearer on every path except ``OPEN_PATHS``.

    Skips the check when no bearer is configured: the network is the
    boundary in that case.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.settings.open or scope["path"] in OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        raw = headers.get(b"authorization", b"").decode("latin-1")
        if find_caller(self.settings, raw) is None:
            logger.warning({"message": "rejected request: missing or wrong token"})
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


async def healthz(request: Request) -> Response:
    return JSONResponse({"ok": True})


mcp.custom_route("/healthz", methods=["GET"])(healthz)


def build_app(settings: Settings | None = None) -> ASGIApp:
    """Build the addon's ASGI app: the MCP routes plus the auth wrapper.

    ``host="0.0.0.0"`` turns off the SDK's loopback-only DNS-rebinding guard:
    a caller reaches this addon by its cluster or ingress hostname, never by
    ``localhost``, and the guard would otherwise reject every real request.
    """
    global _state
    settings = settings or settings_from_env()
    core = CoreSearch(settings.core_url, settings.core_token) if settings.core_url else None
    _state = _State(settings=settings, index=Index(settings.wiki), core=core)
    if not settings.wiki.is_dir():
        logger.warning({"message": "the wiki folder is missing; reads return nothing"})
    logger.info(
        {
            "message": "joshua-mcp ready",
            "callers": sorted(c.name for c in settings.tokens.values()),
            "open": settings.open,
            "semantic_search": core is not None,
        }
    )
    inner = mcp.streamable_http_app(streamable_http_path="/mcp", host="0.0.0.0")
    return BearerAuthMiddleware(inner, settings)
