"""The HTTP API of the chores web UI, mounted at ``/api``.

``server.build_app`` mounts this app on the same Starlette app that serves
``/mcp``. The UI and the API have one origin, so the addon has no CORS
middleware.

Two kinds of route:

- Open routes: each ``GET`` route except ``/transactions``, and
  ``POST /chores/{id}/complete``. The kiosk calls them with no credential.
  The ingress or the docker network is the boundary, as for the UI.
- Manager routes: each other write route, and ``GET /transactions``. A
  manager route needs ``Authorization: Bearer <MANAGER_PIN>``. A missing or
  wrong PIN gets 401. When ``MANAGER_PIN`` is not set, a manager route gets
  503, so a write is never open.

``ADDON_TOKEN`` does not apply here. It guards ``/mcp`` only.

Each handler calls the same service functions and the same output shapes
as the MCP tools in ``server``. An error body is ``{"detail": "<message>"}``.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

import joshua_chores.server as server
from joshua_chores import service
from joshua_chores.log import get_logger

logger = get_logger("chores.api")

# The value of ``transactions.actor`` for a manager write, and for a
# completion from the open route.
MANAGER_ACTOR = "api"
KIOSK_ACTOR = "kiosk"

# The default and the maximum number of rows that a ledger route returns.
LEDGER_DEFAULT = 50
TRANSACTIONS_DEFAULT = 100
ROWS_MAX = 500

Handler = Callable[[Request], Awaitable[Response]]


class BadRequest(Exception):
    """A request that is not valid. The handler answers 400 with the message."""


def _detail(status_code: int, detail: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status_code, headers=headers)


def _error_response(exc: Exception) -> JSONResponse:
    """Change a request or service error to its HTTP status and body."""
    if isinstance(exc, service.NotFound):
        return _detail(404, str(exc))
    if isinstance(exc, service.Cooldown):
        return _detail(409, str(exc), headers={"Retry-After": str(exc.retry_after)})
    return _detail(400, str(exc))


def _handles_errors(handler: Handler) -> Handler:
    """Answer a ``BadRequest`` or a service error with its status and ``{"detail": ...}``."""

    async def wrapped(request: Request) -> Response:
        try:
            return await handler(request)
        except (BadRequest, service.ChoresError) as exc:
            return _error_response(exc)

    return wrapped


def _bearer(request: Request) -> str:
    raw = request.headers.get("authorization", "")
    prefix = "Bearer "
    return raw[len(prefix) :] if raw.startswith(prefix) else ""


def manager(handler: Handler) -> Handler:
    """Require ``Authorization: Bearer <MANAGER_PIN>`` before ``handler`` runs.

    No PIN set gives 503. A missing or wrong PIN gives 401. The check uses
    ``hmac.compare_digest``. The log line of a refusal does not contain the
    PIN that the caller sent.
    """

    async def guarded(request: Request) -> Response:
        pin = server._config().manager_pin
        if not pin:
            return _detail(503, "MANAGER_PIN is not set")
        provided = _bearer(request)
        if not provided or not hmac.compare_digest(provided.encode(), pin.encode()):
            logger.warning(
                {
                    "message": "rejected manager request: missing or wrong PIN",
                    "path": request.url.path,
                }
            )
            return _detail(401, "unauthorized")
        return await handler(request)

    return guarded


# Request bodies


class _Body(BaseModel):
    """A request body. A field that the model does not know is an error."""

    model_config = ConfigDict(extra="forbid", strict=True)


class CompleteBody(_Body):
    note: str | None = None


class NewMember(_Body):
    slug: str
    name: str


class MemberChanges(_Body):
    name: str | None = None
    is_active: bool | None = None
    sort_order: int | None = None


class NewChore(_Body):
    slug: str
    name: str
    points: int
    frequency: str
    next_due_date: date | None = None


class ChoreChanges(_Body):
    name: str | None = None
    points: int | None = None
    frequency: str | None = None
    next_due_date: date | None = None
    is_active: bool | None = None


class LedgerEntry(_Body):
    points: int
    description: str


async def _body[M: _Body](request: Request, model: type[M]) -> M:
    """Read the body as ``model``. An empty body is ``{}``."""
    raw = await request.body()
    if not raw.strip():
        raw = b"{}"
    try:
        data = json.loads(raw)
    except ValueError:
        raise BadRequest("the body must be JSON") from None
    if not isinstance(data, dict):
        raise BadRequest("the body must be a JSON object")
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        error = exc.errors()[0]
        where = ".".join(str(part) for part in error["loc"])
        raise BadRequest(f"{where}: {error['msg']}" if where else error["msg"]) from None


# Query parameters


def _flag(request: Request, name: str) -> bool:
    raw = request.query_params.get(name)
    if raw is None or raw.lower() in ("false", "0", ""):
        return False
    if raw.lower() in ("true", "1"):
        return True
    raise BadRequest(f"{name} must be true or false, not {raw!r}")


def _limit(request: Request, default: int) -> int:
    raw = request.query_params.get("limit")
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if not 1 <= value <= ROWS_MAX:
        raise BadRequest(f"limit must be a whole number from 1 to {ROWS_MAX}, not {raw!r}")
    return value


def _chore_id(request: Request) -> int:
    return request.path_params["chore_id"]


# Output shapes


async def _member_with_balance(session: AsyncSession, member: service.Member) -> dict[str, Any]:
    points = await service.balance(session, member.id)
    return {**server._member(member), "balance": points, "balance_dollars": server._dollars(points)}


async def _slugs(session: AsyncSession) -> dict[int, str]:
    """Return the slug of each member, active or not, by member id."""
    members = await service.list_members(session, include_inactive=True)
    return {member.id: member.slug for member in members}


# Open routes


async def get_settings(request: Request) -> Response:
    config = server._config()
    return JSONResponse(
        {
            "manager_label": config.manager_label,
            "xp_per_dollar": config.xp_per_dollar,
            "cooldown_seconds": config.cooldown_seconds,
            "tz": config.tz.key,
        }
    )


async def list_members(request: Request) -> Response:
    include_inactive = _flag(request, "include_inactive")

    async def work(session: AsyncSession) -> list[dict[str, Any]]:
        members = await service.list_members(session, include_inactive=include_inactive)
        return [await _member_with_balance(session, member) for member in members]

    return JSONResponse(await server._in_session(work))


async def get_member(request: Request) -> Response:
    slug = request.path_params["slug"]

    async def work(session: AsyncSession) -> dict[str, Any]:
        member = await service.get_member_by_slug(session, slug)
        return await _member_with_balance(session, member)

    return JSONResponse(await server._in_session(work))


async def member_ledger(request: Request) -> Response:
    slug = request.path_params["slug"]
    limit = _limit(request, LEDGER_DEFAULT)

    async def work(session: AsyncSession) -> list[dict[str, Any]]:
        member = await service.get_member_by_slug(session, slug)
        rows = await service.ledger(session, member.id, limit=limit)
        return [server._transaction(row) for row in rows]

    return JSONResponse(await server._in_session(work))


async def list_chores(request: Request) -> Response:
    slug = request.query_params.get("slug") or None
    overdue_only = _flag(request, "overdue_only")
    today = server._local_today(server._utc_now())

    async def work(session: AsyncSession) -> dict[str, Any]:
        member_id = None
        if slug is not None:
            member_id = (await service.get_member_by_slug(session, slug)).id
        slugs = await _slugs(session)
        views = await service.list_chores(
            session, member_id=member_id, overdue_only=overdue_only, today=today
        )
        chores = [
            {
                **server._chore(view),
                "slug": slugs[view.member_id],
                "overdue": view.next_due_date <= today,
            }
            for view in views
        ]
        return {"today": today.isoformat(), "chores": chores}

    return JSONResponse(await server._in_session(work))


async def complete_chore(request: Request) -> Response:
    body = await _body(request, CompleteBody)
    work = server._complete_work(_chore_id(request), body.note, KIOSK_ACTOR)
    return JSONResponse(await server._in_session(work))


# Manager routes


async def add_member(request: Request) -> Response:
    body = await _body(request, NewMember)

    async def work(session: AsyncSession) -> dict[str, Any]:
        member = await service.add_member(session, body.slug, body.name)
        return await _member_with_balance(session, member)

    return JSONResponse(await server._in_session(work), status_code=201)


async def change_member(request: Request) -> Response:
    slug = request.path_params["slug"]
    body = await _body(request, MemberChanges)

    async def work(session: AsyncSession) -> dict[str, Any]:
        member = await service.set_member(
            session, slug, name=body.name, is_active=body.is_active, sort_order=body.sort_order
        )
        return await _member_with_balance(session, member)

    return JSONResponse(await server._in_session(work))


async def add_chore(request: Request) -> Response:
    body = await _body(request, NewChore)
    work = server._add_chore_work(
        body.slug, body.name, body.points, body.frequency, body.next_due_date
    )
    return JSONResponse(await server._in_session(work), status_code=201)


async def change_chore(request: Request) -> Response:
    chore_id = _chore_id(request)
    body = await _body(request, ChoreChanges)

    async def work(session: AsyncSession) -> dict[str, Any]:
        chore = await service.update_chore(session, chore_id, **body.model_dump())
        slugs = await _slugs(session)
        return {"slug": slugs[chore.member_id], **server._chore(chore)}

    return JSONResponse(await server._in_session(work))


async def retire_chore(request: Request) -> Response:
    chore_id = _chore_id(request)

    async def work(session: AsyncSession) -> None:
        await service.retire_chore(session, chore_id)

    await server._in_session(work)
    return Response(status_code=204)


async def _ledger_write(request: Request, write: Callable[..., Any]) -> Response:
    body = await _body(request, LedgerEntry)
    slug = request.path_params["slug"]
    work = server._ledger_work(write, slug, body.points, body.description, MANAGER_ACTOR)
    return JSONResponse(await server._in_session(work))


async def award(request: Request) -> Response:
    return await _ledger_write(request, service.award)


async def deduct(request: Request) -> Response:
    return await _ledger_write(request, service.deduct)


async def list_transactions(request: Request) -> Response:
    slug = request.query_params.get("slug") or None
    limit = _limit(request, TRANSACTIONS_DEFAULT)

    async def work(session: AsyncSession) -> list[dict[str, Any]]:
        member_id = None
        if slug is not None:
            member_id = (await service.get_member_by_slug(session, slug)).id
        slugs = await _slugs(session)
        rows = await service.ledger(session, member_id, limit=limit)
        return [{**server._transaction(row), "slug": slugs[row.member_id]} for row in rows]

    return JSONResponse(await server._in_session(work))


def _route(path: str, handler: Handler, methods: list[str], *, guarded: bool = False) -> Route:
    wrapped = _handles_errors(handler)
    return Route(path, manager(wrapped) if guarded else wrapped, methods=methods)


def _routes() -> list[Route]:
    chore = "/chores/{chore_id:int}"
    return [
        _route("/settings", get_settings, ["GET"]),
        _route("/version", server.version, ["GET"]),
        _route("/members", list_members, ["GET"]),
        _route("/members", add_member, ["POST"], guarded=True),
        _route("/members/{slug}", get_member, ["GET"]),
        _route("/members/{slug}", change_member, ["PATCH"], guarded=True),
        _route("/members/{slug}/ledger", member_ledger, ["GET"]),
        _route("/members/{slug}/award", award, ["POST"], guarded=True),
        _route("/members/{slug}/deduct", deduct, ["POST"], guarded=True),
        _route("/chores", list_chores, ["GET"]),
        _route("/chores", add_chore, ["POST"], guarded=True),
        _route(chore, change_chore, ["PATCH", "PUT"], guarded=True),
        _route(chore, retire_chore, ["DELETE"], guarded=True),
        _route(f"{chore}/complete", complete_chore, ["POST"]),
        _route("/transactions", list_transactions, ["GET"], guarded=True),
    ]


def build_api_app() -> Starlette:
    """Build the ``/api`` app. See the module docstring for the two kinds of route."""
    return Starlette(routes=_routes())
