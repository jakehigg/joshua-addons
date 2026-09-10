"""The vinyl addon: the browse interface, the bundle, and MCP, on one port.

The shape is the hello addon's. MCP is at ``/mcp``, the streamable-HTTP
transport the joshua-ai gateway expects from a ``type: http`` upstream.
``GET /healthz`` stays open. ``/mcp`` needs ``Authorization: Bearer
<ADDON_TOKEN>`` when ``ADDON_TOKEN`` is set. The interface at ``/``, the
bundle at ``/bundle``, the art at ``/art``, and ``/api/status`` are open, the
same posture as the pantry UI: the docker network or the ingress is the
boundary, not this addon.
"""

from __future__ import annotations

import asyncio
import hmac
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from joshua_vinyl import db, identify, intake
from joshua_vinyl import query as query_bundle
from joshua_vinyl.bundle import read_index
from joshua_vinyl.config import Settings, load_shelf_config, settings_from_env
from joshua_vinyl.discogs import DiscogsClient
from joshua_vinyl.log import get_logger
from joshua_vinyl.musicbrainz import MusicBrainzClient
from joshua_vinyl.scheduler import run_daily
from joshua_vinyl.sync import META_LAST_RESULT, META_LAST_SYNC

logger = get_logger("vinyl")

PROTECTED_PREFIX = "/mcp"

# Set by ``build_app``. The lifespan and the status tool read it.
_settings: Settings | None = None


def _current_settings() -> Settings:
    if _settings is None:
        raise RuntimeError("build_app has not run")
    return _settings


def status(settings: Settings) -> dict[str, Any]:
    """The sync state: how many records, when the last sync ran, and how it ended."""
    index = read_index(settings.bundle_dir)
    result: dict[str, Any] = {
        "records": index["count"] if index else 0,
        "bundle_generated_at": index["generated_at"] if index else None,
        "last_sync": None,
        "last_result": None,
    }
    if settings.db_path.is_file():
        conn = db.connect(settings.db_path)
        try:
            result["last_sync"] = db.get_meta(conn, META_LAST_SYNC)
            result["last_result"] = db.get_meta(conn, META_LAST_RESULT)
        finally:
            conn.close()
    return result


def _scheduled_sync() -> None:
    from joshua_vinyl.cli import sync_once

    sync_once(_current_settings())


@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[dict[str, Any]]:
    """Start the nightly sync loop when a schedule and a token are both set."""
    settings = _current_settings()
    task: asyncio.Task[int] | None = None
    if settings.sync_time and settings.discogs_token:
        task = asyncio.create_task(run_daily(settings.sync_time, _scheduled_sync))
    else:
        logger.info({"message": "nightly sync off: no VINYL_SYNC_TIME or no DISCOGS_TOKEN"})
    try:
        yield {}
    finally:
        if task is not None:
            task.cancel()


mcp = MCPServer(name="vinyl", lifespan=lifespan)


@mcp.tool()
def vinyl_status() -> dict[str, Any]:
    """Report the sync state: record count, last sync time, and last result."""
    return status(_current_settings())


def _index_or_none() -> dict[str, Any] | None:
    return read_index(_current_settings().bundle_dir)


NO_BUNDLE = {"error": "The collection is not synced yet. No record is available."}


@mcp.tool()
def vinyl_search(
    query: str | None = None,
    genre: str | None = None,
    decade: int | None = None,
    year: int | None = None,
    section: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Find records in the house collection.

    ``query`` matches the album title, the artist, or the label, and it
    tolerates a missing "The". Use a filter with no query to list a part of
    the collection, for example every Jazz record, or everything from the
    1970s. The answer gives the number found, and up to ``limit`` records
    with the shelf section of each.
    """
    index = _index_or_none()
    if index is None:
        return NO_BUNDLE
    return query_bundle.search(
        index, query, genre=genre, decade=decade, section=section, year=year, limit=limit
    )


@mcp.tool()
def vinyl_details(record_id: int) -> dict[str, Any]:
    """Report one record in full, by the id that ``vinyl_search`` gives.

    The answer holds the shelf section, the label and catalog number, the
    format, the country, the genres and styles, the tracklist, and the
    lowest listed price with the date it was checked. Never present the
    price as live: give the date with it.
    """
    record = query_bundle.detail(_current_settings().bundle_dir, record_id)
    if record is None:
        return {"error": f"No record with id {record_id} is in the collection."}
    return record


@mcp.tool()
def vinyl_stats() -> dict[str, Any]:
    """Report the shape of the collection: how many records, of what, and from when.

    Use it for a question about the size of the collection, the genres in
    it, the decades it covers, or which shelf sections are fullest.
    """
    index = _index_or_none()
    if index is None:
        return NO_BUNDLE
    return query_bundle.stats(index)


@mcp.tool()
def vinyl_recent(limit: int = 10) -> dict[str, Any]:
    """List the records added most recently, newest first.

    Use it for a question about what is new, or what came in this month.
    """
    index = _index_or_none()
    if index is None:
        return NO_BUNDLE
    return query_bundle.recent(index, limit)


@mcp.tool()
def vinyl_pick(
    genre: str | None = None,
    decade: int | None = None,
    section: str | None = None,
    exclude_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Choose one record to play, with a reason and the shelf section.

    The choice is always a record the house owns. Give ``genre`` or
    ``decade`` for a mood. Give ``exclude_ids`` to keep a record that was
    suggested already out of the answer.
    """
    index = _index_or_none()
    if index is None:
        return NO_BUNDLE
    return query_bundle.pick(
        index, genre=genre, decade=decade, section=section, exclude=exclude_ids
    )


WRITE_HELP = (
    "The addon has no Discogs token, so it can read the shelf but it cannot "
    "search Discogs or add a record. Set DISCOGS_TOKEN and DISCOGS_USERNAME."
)


@contextmanager
def _discogs() -> Iterator[tuple[Any, str]]:
    """A Discogs client and the account name, closed at the end of the tool."""
    settings = _current_settings()
    if not settings.discogs_token:
        raise ToolError(WRITE_HELP)
    client = DiscogsClient(settings.discogs_token, settings.user_agent)
    try:
        username = settings.discogs_username or client.identity()["username"]
        yield client, username
    finally:
        client.close()


@contextmanager
def _database() -> Iterator[Any]:
    conn = db.connect(_current_settings().db_path)
    try:
        yield conn
    finally:
        conn.close()


@mcp.tool()
def vinyl_owned(
    artist: str | None = None,
    title: str | None = None,
    release_id: int | None = None,
    master_id: int | None = None,
) -> dict[str, Any]:
    """Answer whether the house owns an album already, before somebody buys it.

    This is the question asked in a shop with a record in hand, so it reads
    the local shelf only and answers at once, with no network. A match on the
    title alone is a maybe, never a yes.
    """
    index = _index_or_none()
    if index is None:
        return NO_BUNDLE
    return query_bundle.owned(
        index, artist=artist, title=title, release_id=release_id, master_id=master_id
    )


@mcp.tool()
def vinyl_lookup(
    artist: str | None = None,
    title: str | None = None,
    catalog_no: str | None = None,
    barcode: str | None = None,
    label: str | None = None,
    matrix: str | None = None,
    year: int | None = None,
    country: str | None = None,
    tracks: list[str] | None = None,
) -> dict[str, Any]:
    """Find the release on Discogs from what you read on the record itself.

    Read the photograph yourself and pass what is printed, not what you know.
    Ask for a photograph of the disc **label**, which carries the catalog
    number, the label name and the pressing details. Cover art identifies
    nothing: a bowler-hat sleeve once read as the wrong album entirely, and
    the label photograph settled it.

    Give every field you can see. The catalog number goes in as printed,
    including a leading X. Give the track titles as well: a tracklist never
    names a release, but it is the one thing a wrong match cannot fake.

    The answer holds at most three candidates, each with the full format
    line, the release notes, the country and the year. Read those four before
    you choose. A candidate that says Picture Disc, Test Pressing or
    Quadraphonic is usually the wrong pressing, and a promotional copy often
    hides in the notes while the format line stays ordinary.

    Nothing here is a decision. Call vinyl_label_images on the candidates and
    compare the pictures against the photograph, which is the check that
    catches most wrong picks. An empty answer is usually a misread or a
    different spelling of the artist, not a record Discogs lacks.
    """
    with _database() as conn:
        owned_releases, owned_masters = db.collection_ids(conn)
    with _discogs() as (client, _username):
        result = intake.find_candidates(
            client,
            artist=artist,
            title=title,
            catalog_no=catalog_no,
            barcode=barcode,
            label=label,
            year=year,
            country=country,
            tracks=tracks,
            owned_release_ids=owned_releases,
            owned_master_ids=owned_masters,
        )
    if matrix:
        result["matrix_read"] = matrix
        result["matrix_note"] = (
            "Discogs stores the label matrix as 'Matrix / Runout (Label side A)'. "
            "Compare it against each candidate by eye; a matching matrix narrows "
            "the field and does not settle it, because two releases can share one."
        )
    return result


@mcp.tool()
def vinyl_label_images(release_id: int, limit: int = 3) -> list[Image]:
    """Show the disc labels Discogs holds for one release, as pictures.

    Compare them against the photograph of the record in hand. Look at the
    layout first, then the text, then the colour: where the copyright sits,
    whether STEREO is printed, which side of the spindle the catalog number
    is on. Lighting changes colour and does not change layout.

    This comparison overturned nineteen otherwise clean matches across three
    batches, and no automatic check caught any of them.
    """
    with _discogs() as (client, _username):
        release = client.release(int(release_id))
        urls = identify.label_image_urls(release, limit=max(1, min(int(limit), 5)))
        images: list[Image] = []
        for url in urls:
            images.append(Image(data=client.download(url), format="jpeg"))
    if not images:
        raise ToolError(f"Discogs has no image for release {release_id}.")
    return images


@mcp.tool()
def vinyl_add(
    release_id: int,
    note: str | None = None,
    pressing_confirmed: bool = False,
    confirm: bool = False,
    allow_duplicate: bool = False,
) -> dict[str, Any]:
    """Put one record in the collection. It plans first and writes only on confirm.

    Call it once with no confirm and show the person the plan: the release,
    the format line, the release notes, whether the album is on the shelf
    already, and the note that would be written. Call it again with confirm
    once they agree. Never confirm on your own.

    Set pressing_confirmed only when the evidence names this exact pressing,
    which usually means a barcode, a label matrix, or a label picture that
    matches. Otherwise the note carries pressing-unconfirmed, which is how a
    later pass finds the records that still need one.

    The note is for what the record is and how it was identified. It is cut
    to 255 characters, so put the important half first.

    After a write the record is on the shelf page at once, and the answer
    says which section to file it under.
    """
    settings = _current_settings()
    config = load_shelf_config(settings.config_path)
    with _database() as conn, _discogs() as (client, username):
        musicbrainz = MusicBrainzClient(settings.user_agent)
        try:
            return intake.add_record(
                conn,
                client,
                username=username,
                release_id=int(release_id),
                config=config,
                art_dir=settings.art_dir,
                bundle_dir=settings.bundle_dir,
                musicbrainz=musicbrainz,
                note=note,
                pressing_confirmed=pressing_confirmed,
                confirm=confirm,
                allow_duplicate=allow_duplicate,
                currency=settings.currency,
            )
        finally:
            musicbrainz.close()


class BearerAuthMiddleware:
    """Require ``Authorization: Bearer <token>`` on ``PROTECTED_PREFIX`` only.

    Skips the check entirely when ``token`` is ``None`` (no ``ADDON_TOKEN``
    configured): the docker network is the boundary in that case.
    """

    def __init__(self, app: ASGIApp, token: str | None) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        protected = path == PROTECTED_PREFIX or path.startswith(PROTECTED_PREFIX + "/")
        if scope["type"] != "http" or self.token is None or not protected:
            await self.app(scope, receive, send)
            return

        headers = dict(scope["headers"])
        raw = headers.get(b"authorization", b"").decode("latin-1")
        prefix = "Bearer "
        provided = raw[len(prefix) :] if raw.startswith(prefix) else ""
        if not provided or not hmac.compare_digest(provided, self.token):
            logger.warning({"message": "rejected request: missing or wrong token"})
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


async def healthz(request: Request) -> Response:
    return JSONResponse({"ok": True})


async def api_status(request: Request) -> Response:
    return JSONResponse(status(_current_settings()))


mcp.custom_route("/healthz", methods=["GET"])(healthz)
mcp.custom_route("/api/status", methods=["GET"])(api_status)


def build_app() -> ASGIApp:
    """Build the ASGI app: MCP, the status route, the bundle, the art, and the UI.

    ``host="0.0.0.0"`` turns off the SDK's loopback-only DNS-rebinding guard:
    a caller reaches this addon by its compose or cluster hostname, never by
    ``localhost``, and the guard would otherwise reject every real request.

    Starlette tries routes in registration order, so ``/mcp``, ``/healthz``,
    and ``/api/status`` match first, then the two data mounts, then the UI at
    ``/`` last. The bundle and the art directories are created here, empty,
    because they do not exist before the first sync and a static mount needs
    a directory that exists.
    """
    global _settings
    _settings = settings_from_env()
    _settings.bundle_dir.mkdir(parents=True, exist_ok=True)
    _settings.art_dir.mkdir(parents=True, exist_ok=True)
    inner = mcp.streamable_http_app(streamable_http_path="/mcp", host="0.0.0.0")
    inner.mount("/bundle", StaticFiles(directory=str(_settings.bundle_dir)))
    inner.mount("/art", StaticFiles(directory=str(_settings.art_dir)))
    if _settings.static_dir.is_dir():
        inner.mount("/", StaticFiles(directory=str(_settings.static_dir), html=True))
    token = os.environ.get("ADDON_TOKEN") or None
    return BearerAuthMiddleware(inner, token)
