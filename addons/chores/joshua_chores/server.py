"""The chores addon server: MCP at ``/mcp``, ``/healthz``, and the built UI at ``/``.

The addon follows the contract in ``docs/adding-an-addon.md``. It serves MCP
at ``/mcp`` over streamable HTTP, the transport the joshua-ai gateway expects
from a ``type: http`` upstream. ``GET /healthz`` stays open. When
``ADDON_TOKEN`` is set, ``/mcp`` needs ``Authorization: Bearer <ADDON_TOKEN>``.
When ``ADDON_TOKEN`` is not set, the docker network is the boundary.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from joshua_chores import importer, service
from joshua_chores import settings as settings_module
from joshua_chores.database import build_engine, build_sessionmaker
from joshua_chores.log import get_logger
from joshua_chores.migrations import run_migrations
from joshua_chores.models import Transaction

logger = get_logger("chores")

# Only ``/mcp`` needs a bearer token. ``/healthz`` and the UI at ``/`` stay
# open.
PROTECTED_PREFIX = "/mcp"

# The value of ``transactions.actor`` for each write from a tool. The gateway
# does not tell the addon who calls.
ACTOR = "mcp"

# The number of ledger rows that ``get_balance`` returns.
LEDGER_ROWS = 10

# ``lifespan`` sets these at startup and clears them at shutdown. A tool that
# runs outside the lifespan gets an error, not a missing database.
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_settings: settings_module.Settings | None = None


def _session_factory() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("the database is not ready; the server lifespan has not started")
    return _sessionmaker


def _config() -> settings_module.Settings:
    if _settings is None:
        raise RuntimeError("the settings are not ready; the server lifespan has not started")
    return _settings


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _local_today(now: datetime) -> date:
    return now.astimezone(_config().tz).date()


@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[dict[str, Any]]:
    """Check the settings, build the engine, run the migrations, and open the session factory.

    This runs one time, when the streamable-HTTP session manager starts (see
    ``build_app``). It reads the environment here, not at import time, so a
    caller (a test, for example) can set it first. A bad setting raises
    ``ValueError`` and stops the startup.
    """
    global _sessionmaker, _settings
    loaded = settings_module.load()
    engine = build_engine()
    await run_migrations(engine)
    _settings = loaded
    _sessionmaker = build_sessionmaker(engine)
    try:
        yield {}
    finally:
        _sessionmaker = None
        _settings = None
        await engine.dispose()


mcp = MCPServer(name="chores", lifespan=lifespan)


async def _run[T](work: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run ``work`` in a new session. Change each service error to a ``ToolError``."""
    async with _session_factory()() as session:
        try:
            return await work(session)
        except service.Cooldown as exc:
            raise ToolError(
                f"the chore was completed recently; try again in {exc.retry_after} seconds"
            ) from None
        except service.ChoresError as exc:
            raise ToolError(str(exc)) from None


def _iso(value: date | datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _parse_date(value: str | None, what: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ToolError(f"{what} must be an ISO date (YYYY-MM-DD), not {value!r}") from None


def _dollars(points: int) -> str | None:
    return settings_module.format_dollars(points, _config().xp_per_dollar)


def _transaction(row: Transaction) -> dict[str, Any]:
    return {
        "id": row.id,
        "amount": row.amount,
        "description": row.description,
        "source": row.source,
        "created_at": _iso(row.created_at),
    }


def _chore(chore: service.Chore | service.ChoreView) -> dict[str, Any]:
    return {
        "id": chore.id,
        "name": chore.name,
        "points": chore.points,
        "frequency": chore.frequency,
        "is_active": chore.is_active,
        "next_due_date": _iso(chore.next_due_date),
        "last_completed_at": _iso(chore.last_completed_at),
    }


@mcp.tool()
async def list_members() -> dict[str, Any]:
    """List the active members of the household, with the XP balance of each.

    Call this tool first to learn the slugs. Each other tool that is about a
    member takes a member's slug, not the name.

    ``balance_dollars`` is the balance as a dollar string, or null when the
    addon shows no dollar values.
    """

    async def work(session: AsyncSession) -> dict[str, Any]:
        rows = []
        for member in await service.list_members(session):
            points = await service.balance(session, member.id)
            rows.append(
                {
                    "slug": member.slug,
                    "name": member.name,
                    "balance": points,
                    "balance_dollars": _dollars(points),
                }
            )
        return {"members": rows}

    return await _run(work)


@mcp.tool()
async def list_chores(slug: str, overdue_only: bool = False) -> dict[str, Any]:
    """List the active chores of one member, sorted by due date. This tool does not write.

    ``next_due_date`` is the due date of the current period. ``overdue`` is
    true when the chore is due today or earlier, in the time zone of the
    household. Use the ``id`` of a chore with ``complete_chore``.

    Args:
        slug: A member's slug, from ``list_members``.
        overdue_only: When true, list only the chores that are due today or earlier.
    """
    now = _utc_now()
    today = _local_today(now)

    async def work(session: AsyncSession) -> dict[str, Any]:
        member = await service.get_member_by_slug(session, slug)
        views = await service.list_chores(
            session, member_id=member.id, overdue_only=overdue_only, today=today
        )
        chores = [{**_chore(view), "overdue": view.next_due_date <= today} for view in views]
        return {"slug": member.slug, "today": today.isoformat(), "chores": chores}

    return await _run(work)


@mcp.tool()
async def complete_chore(chore_id: int, note: str | None = None) -> dict[str, Any]:
    """Mark a chore as done, and award its XP to the member of the chore.

    Get the ``chore_id`` from ``list_chores``. A recurring chore moves to its
    next due date. A one-off chore is retired. A second completion of the
    same chore within the cooldown fails, and the error gives the number of
    seconds to wait.

    Args:
        chore_id: The id of the chore.
        note: An optional note to keep with the completion.
    """
    now = _utc_now()
    today = _local_today(now)
    cooldown = _config().cooldown_seconds

    async def work(session: AsyncSession) -> dict[str, Any]:
        result = await service.complete_chore(
            session,
            chore_id,
            now=now,
            today=today,
            note=note,
            cooldown_seconds=cooldown,
            actor=ACTOR,
        )
        chore = await service.get_chore(session, chore_id)
        return {
            "chore_id": result.chore_id,
            "chore_name": chore.name,
            "completion_id": result.completion_id,
            "points_awarded": result.points_awarded,
            "balance": result.balance,
            "balance_dollars": _dollars(result.balance),
            "next_due_date": _iso(result.next_due_date),
            "is_active": result.is_active,
        }

    return await _run(work)


@mcp.tool()
async def get_balance(slug: str) -> dict[str, Any]:
    """Get the XP balance of one member and the 10 newest ledger rows, newest first.

    Args:
        slug: A member's slug, from ``list_members``.
    """

    async def work(session: AsyncSession) -> dict[str, Any]:
        member = await service.get_member_by_slug(session, slug)
        points = await service.balance(session, member.id)
        rows = await service.ledger(session, member.id, limit=LEDGER_ROWS)
        return {
            "slug": member.slug,
            "balance": points,
            "balance_dollars": _dollars(points),
            "ledger": [_transaction(row) for row in rows],
        }

    return await _run(work)


async def _ledger_write(
    write: Callable[..., Awaitable[service.LedgerResult]],
    slug: str,
    points: int,
    description: str,
) -> dict[str, Any]:
    async def work(session: AsyncSession) -> dict[str, Any]:
        member = await service.get_member_by_slug(session, slug)
        result = await write(session, member.id, points, description, actor=ACTOR)
        return {
            "slug": member.slug,
            "transaction": _transaction(result.transaction),
            "balance": result.balance,
            "balance_dollars": _dollars(result.balance),
        }

    return await _run(work)


@mcp.tool()
async def award_xp(slug: str, points: int, description: str) -> dict[str, Any]:
    """Give XP to a member, for example a bonus. This is a manager action.

    Args:
        slug: A member's slug, from ``list_members``.
        points: The XP to add. It must be a whole number more than 0.
        description: Why the member gets the XP.
    """
    return await _ledger_write(service.award, slug, points, description)


@mcp.tool()
async def deduct_xp(slug: str, points: int, description: str) -> dict[str, Any]:
    """Remove XP from a member, for example when the member spends it. This is a manager action.

    The balance can go below 0.

    Args:
        slug: A member's slug, from ``list_members``.
        points: The XP to remove. It must be a whole number more than 0.
        description: Why the XP goes out.
    """
    return await _ledger_write(service.deduct, slug, points, description)


@mcp.tool()
async def add_chore(
    slug: str,
    name: str,
    points: int,
    frequency: str,
    next_due_date: str | None = None,
) -> dict[str, Any]:
    """Add a chore to an active member. This is a manager action.

    Args:
        slug: A member's slug, from ``list_members``.
        name: The name of the chore.
        points: The XP that one completion gives. A whole number more than 0.
        frequency: ``daily``, ``weekly``, ``monthly``, or ``one_off``.
        next_due_date: The first due date, as YYYY-MM-DD. The default is
            today in the time zone of the household.
    """
    due = _parse_date(next_due_date, "next_due_date") or _local_today(_utc_now())

    async def work(session: AsyncSession) -> dict[str, Any]:
        member = await service.get_member_by_slug(session, slug)
        chore = await service.add_chore(session, member.id, name, points, frequency, due)
        return {"slug": member.slug, **_chore(chore)}

    return await _run(work)


@mcp.tool()
async def update_chore(
    chore_id: int,
    name: str | None = None,
    points: int | None = None,
    frequency: str | None = None,
    next_due_date: str | None = None,
) -> dict[str, Any]:
    """Change a chore. This is a manager action. A field that you do not give does not change.

    Args:
        chore_id: The id of the chore.
        name: The new name.
        points: The new XP value. A whole number more than 0.
        frequency: ``daily``, ``weekly``, ``monthly``, or ``one_off``.
        next_due_date: The new due date, as YYYY-MM-DD.
    """
    due = _parse_date(next_due_date, "next_due_date")

    async def work(session: AsyncSession) -> dict[str, Any]:
        chore = await service.update_chore(
            session, chore_id, name=name, points=points, frequency=frequency, next_due_date=due
        )
        return _chore(chore)

    return await _run(work)


@mcp.tool()
async def retire_chore(chore_id: int) -> dict[str, Any]:
    """Retire a chore, so that it is not in the lists. This is a manager action.

    The addon keeps the chore and its history.

    Args:
        chore_id: The id of the chore.
    """

    async def work(session: AsyncSession) -> dict[str, Any]:
        return _chore(await service.retire_chore(session, chore_id))

    return await _run(work)


def _member(member: service.Member) -> dict[str, Any]:
    return {
        "slug": member.slug,
        "name": member.name,
        "is_active": member.is_active,
        "sort_order": member.sort_order,
    }


@mcp.tool()
async def add_member(slug: str, name: str) -> dict[str, Any]:
    """Add a member to the household. This is a manager action.

    Args:
        slug: The new member's slug: 1 to 32 lowercase letters, digits, or
            hyphens. It cannot change later.
        name: The name to show.
    """

    async def work(session: AsyncSession) -> dict[str, Any]:
        return _member(await service.add_member(session, slug, name))

    return await _run(work)


@mcp.tool()
async def set_member(
    slug: str,
    name: str | None = None,
    is_active: bool | None = None,
    sort_order: int | None = None,
) -> dict[str, Any]:
    """Change the name, the active flag, or the sort order of a member. This is a manager action.

    A field that you do not give does not change. An inactive member is not
    in ``list_members``.

    Args:
        slug: A member's slug, from ``list_members``.
        name: The new name.
        is_active: False to hide the member, true to show the member again.
        sort_order: The position of the member in the lists. Lower comes first.
    """

    async def work(session: AsyncSession) -> dict[str, Any]:
        member = await service.set_member(
            session, slug, name=name, is_active=is_active, sort_order=sort_order
        )
        return _member(member)

    return await _run(work)


@mcp.tool()
async def import_data(
    version: int,
    members: list[dict[str, Any]],
    chores: list[dict[str, Any]] | None = None,
    completions: list[dict[str, Any]] | None = None,
    transactions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Load members, chores, completions, and ledger rows in bulk from an export.

    Read ``docs/import.md`` of the addon for the format. The tool checks the
    full batch before it writes. One bad row stops the call, and the error
    gives the path of each problem. A second import of the same batch
    changes nothing. The result gives the ``created`` and ``skipped`` counts
    of each table and the balance of each member in the batch.

    Args:
        version: The format version. It must be 1.
        members: Member rows: slug, name, is_active, sort_order.
        chores: Chore rows: member (a slug), name, points, frequency,
            is_active, next_due_date, last_completed_at, created_at, external_id.
        completions: Completion rows: external_id, external_chore_id or
            member and chore (a name), completed_at, points_awarded, note.
        transactions: Ledger rows: external_id, member, amount, description,
            source, created_at, completion_external_id.
    """

    async def work(session: AsyncSession) -> dict[str, Any]:
        return await importer.import_data(
            session, version, members, chores, completions, transactions
        )

    return await _run(work)


class BearerAuthMiddleware:
    """Require ``Authorization: Bearer <token>`` on ``PROTECTED_PREFIX`` only.

    ``/healthz`` and the UI at ``/`` stay open. When ``token`` is ``None``
    (no ``ADDON_TOKEN``), the middleware checks nothing: the docker network
    is the boundary.
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


mcp.custom_route("/healthz", methods=["GET"])(healthz)


def build_app() -> ASGIApp:
    """Build the ASGI app of the addon: MCP, the built UI, and the auth wrapper.

    ``host="0.0.0.0"`` turns off the loopback-only DNS-rebinding guard of the
    SDK. A caller reaches this addon by its compose or cluster hostname (for
    example ``http://chores:8000/mcp``), never by ``localhost``, and the guard
    would refuse each real request.

    The built UI mounts on the ``Starlette`` instance that the MCP SDK returns,
    after ``/mcp`` and ``/healthz``. Starlette tries routes in the order they
    were added, so those two match first and the catch-all mount at ``/``
    matches last. The mount does not change the lifespan of that instance, so
    the database opens the same way with or without the UI.
    """
    from joshua_chores.static import build_static_app

    inner = mcp.streamable_http_app(streamable_http_path="/mcp", host="0.0.0.0")
    static_app = build_static_app()
    if static_app is not None:
        inner.mount("/", static_app)

    token = os.environ.get("ADDON_TOKEN") or None
    return BearerAuthMiddleware(inner, token)
