"""The pantry addon: MCP tools for a receipt-first grocery pantry.

The addon is a copy of the hello addon's shape (``docs/adding-an-addon.md``):
it serves MCP at ``/mcp``, the streamable-HTTP transport the joshua-ai gateway
expects from a ``type: http`` upstream. ``GET /healthz`` stays open. Every
other route needs ``Authorization: Bearer <ADDON_TOKEN>`` when ``ADDON_TOKEN``
is set; with no ``ADDON_TOKEN``, the docker network is the boundary.

The primary tool is ``record_purchase``: it turns a receipt (one date, many
line items) into pantry state, resolving each name against existing items and
aliases before it creates anything new. The rest of the tools read that state
back, or make small manual corrections to it.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from statistics import mean
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from joshua_pantry import inventory as inv_service
from joshua_pantry.database import build_engine, build_sessionmaker
from joshua_pantry.log import get_logger
from joshua_pantry.migrations import run_migrations
from joshua_pantry.models import ConsumptionEvent, Inventory, Item, ItemAlias, PurchaseRecord
from joshua_pantry.purchases import upsert_purchase
from joshua_pantry.resolution import get_aliases_by_item, normalize, resolve_item

logger = get_logger("pantry")

OPEN_PATHS = frozenset({"/healthz"})

# Set by ``lifespan`` on startup, cleared on shutdown. A tool called before
# startup (or after shutdown) fails loudly instead of touching no database.
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _session_factory() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("the database is not ready; the server lifespan has not started")
    return _sessionmaker


@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[dict[str, Any]]:
    """Build the engine, run migrations, and open the session factory.

    Runs once, when the streamable-HTTP session manager starts (see
    ``build_app``). ``DATABASE_URL``/``PANTRY_DB`` are read here, not at
    import time, so a caller (a test, for example) can set them first.
    """
    global _sessionmaker
    engine = build_engine()
    await run_migrations(engine)
    _sessionmaker = build_sessionmaker(engine)
    try:
        yield {}
    finally:
        _sessionmaker = None
        await engine.dispose()


mcp = MCPServer(name="pantry", lifespan=lifespan)


def _parse_purchased_at(value: str) -> datetime:
    """Parse a receipt date into a UTC-aware ``datetime``.

    Accepts an ISO timestamp or a bare ``YYYY-MM-DD`` date. Raises
    ``ToolError`` with the accepted formats when the value does not parse.
    """
    if not value or not value.strip():
        raise ToolError("purchased_at is required. Use an ISO timestamp or YYYY-MM-DD.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ToolError(
            f"Could not parse purchased_at '{value}'. Use an ISO timestamp or YYYY-MM-DD."
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_target_day(value: str) -> date:
    """Parse a purchase date to look up, as a bare ``date`` (the UTC day)."""
    try:
        return datetime.fromisoformat(value).date()
    except ValueError as exc:
        raise ToolError(
            f"Could not parse purchased_at '{value}'. Use an ISO timestamp or YYYY-MM-DD."
        ) from exc


class PurchaseLine(BaseModel):
    """One line of a receipt."""

    name: str
    cost: float | None = None
    sku: str | None = None
    upc: str | None = None
    quantity: float | None = None
    store: str | None = None


@mcp.tool()
async def record_purchase(
    items: list[PurchaseLine],
    purchased_at: str,
    store: str | None = None,
) -> dict[str, Any]:
    """Record a receipt: every item bought, on one purchase date.

    Use this after a shopping trip, from a receipt or a memory of the trip.
    Read every line on the receipt and pass all of them in one call — one
    receipt is one call, with one purchase date for every line. Use the
    date printed on the receipt, not today's date, unless the receipt has
    no date. Include the SKU and UPC digits when the receipt prints them;
    they help future lookups. Do not guess a line that is hard to read —
    ask the person what it says instead.

    A name that matches a tracked item, or one of its aliases, records onto
    that item. A name that matches nothing creates a new tracked item, so a
    receipt name does not need to already match the pantry. Recording the
    same item twice on the same date merges into one purchase record; a
    later call can still fill in a SKU or UPC the first call left out.

    Args:
        items: One entry per receipt line: name (required, non-empty), and
            the optional cost, sku, upc, quantity, and store.
        purchased_at: The receipt date — an ISO timestamp or YYYY-MM-DD.
            This date applies to every item in the call.
        store: The store name for the whole receipt. An item's own store
            overrides this.
    """
    if not items:
        raise ToolError("items is empty; record_purchase needs at least one line item.")

    when = _parse_purchased_at(purchased_at)

    lines: list[dict[str, Any]] = []
    created_items: list[str] = []
    touched: set[int] = set()

    async with _session_factory()() as session:
        for entry in items:
            name = entry.name.strip()
            if not name:
                raise ToolError("Every item needs a non-empty name.")

            match = await resolve_item(session, name)
            item_created = False
            if match is None:
                display = name.lower()
                match = Item(
                    name=display,
                    normalized=normalize(name),
                    first_seen=when,
                    last_seen=when,
                    is_tracked=True,
                )
                session.add(match)
                await session.flush()
                created_items.append(match.name.title())
                item_created = True
            else:
                match.is_tracked = True
                # Don't move last_seen backward when back-filling a past date.
                if match.last_seen is None or when > match.last_seen:
                    match.last_seen = when

            effective_store = entry.store if entry.store else store
            rec, purchase_created = await upsert_purchase(
                session,
                match.id,
                when,
                source="manual",
                unit_cost=entry.cost,
                store=effective_store,
                sku=entry.sku,
                upc=entry.upc,
                quantity=entry.quantity,
            )
            if match.last_purchased_at is None or when > match.last_purchased_at:
                match.last_purchased_at = when

            lines.append(
                {
                    "name": name,
                    "resolved_to": match.name.title(),
                    "created": item_created,
                    "merged_same_day": not purchase_created,
                    "cost": rec.unit_cost,
                    "sku": rec.sku,
                    "upc": rec.upc,
                    "quantity": rec.quantity,
                    "store": rec.store,
                    "purchased_at": rec.purchased_at.isoformat(),
                }
            )
            touched.add(match.id)

        await session.commit()

        for item_id in touched:
            await inv_service.recalculate_inventory(session, item_id)

    return {
        "purchased_at": when.date().isoformat(),
        "items": lines,
        "created_items": created_items,
    }


@mcp.tool()
async def get_inventory(status_filter: str | None = None) -> list[dict[str, Any]]:
    """List every tracked item's inventory status.

    Each item carries its status, category, last_purchased_at, estimated
    depletion, and avg_cycle_days. Aliases, when the item has any, show
    alternate names (e.g. receipt names) that resolve to it — check these
    before treating a receipt name as a new item.

    Args:
        status_filter: Optional filter — "in_stock", "likely_depleted",
            "out_of_stock", or "unknown". Returns every item when omitted.
    """
    async with _session_factory()() as session:
        return await inv_service.get_all_inventory_status(session, status_filter)


@mcp.tool()
async def get_item_history(item_name: str) -> dict[str, Any]:
    """Get purchase history and frequency data for one item.

    The lookup also matches aliases; matched_via says whether the name hit
    the item's own name, an alias, or a fuzzy match. recent_purchases
    carries the sku, upc, and quantity recorded on each purchase, when set.

    Args:
        item_name: The item to look up (alias and fuzzy matched).
    """
    async with _session_factory()() as session:
        match = await resolve_item(session, item_name)
        if match is None:
            raise ToolError(f"Item '{item_name}' not found.")

        pr_result = await session.execute(
            select(PurchaseRecord)
            .where(PurchaseRecord.item_id == match.id)
            .order_by(PurchaseRecord.purchased_at.desc())
            .limit(20)
        )
        purchases = pr_result.scalars().all()

        inv_result = await session.execute(select(Inventory).where(Inventory.item_id == match.id))
        inv = inv_result.scalar_one_or_none()

        alias_result = await session.execute(
            select(ItemAlias).where(ItemAlias.item_id == match.id).order_by(ItemAlias.alias)
        )
        aliases = alias_result.scalars().all()

        query_norm = normalize(item_name)
        if query_norm == match.normalized:
            matched_via = "name"
        elif query_norm in {a.normalized for a in aliases}:
            matched_via = "alias"
        else:
            matched_via = "fuzzy"

        costs = [p.unit_cost for p in purchases if p.unit_cost is not None]

        return {
            "item": match.name.title(),
            "aliases": [a.alias.title() for a in aliases],
            "matched_via": matched_via,
            "preferred_store": match.preferred_store,
            "status": inv_service.get_item_status(inv),
            "avg_cycle_days": inv.avg_cycle_days if inv else None,
            "last_purchased_at": (
                inv.last_purchased_at.isoformat() if inv and inv.last_purchased_at else None
            ),
            "estimated_depletion": (
                inv.estimated_depletion.isoformat() if inv and inv.estimated_depletion else None
            ),
            "purchase_count": len(purchases),
            "avg_cost": round(mean(costs), 2) if costs else None,
            "last_cost": costs[0] if costs else None,
            "recent_purchases": [
                {
                    "purchased_at": p.purchased_at.isoformat(),
                    "unit_cost": p.unit_cost,
                    "store": p.store,
                    "sku": p.sku,
                    "upc": p.upc,
                    "quantity": p.quantity,
                }
                for p in purchases
            ],
        }


@mcp.tool()
async def get_item_cost(item_names: list[str]) -> dict[str, Any]:
    """Get the average and most recent cost for one or more items.

    Use this to estimate the cost of a planned trip: look up each item on
    the list and sum the average costs. An item with no priced purchases
    returns null costs.

    Args:
        item_names: Item names to look up (alias and fuzzy matched).
    """
    out: list[dict[str, Any]] = []
    async with _session_factory()() as session:
        for name in item_names:
            match = await resolve_item(session, name)
            if match is None:
                out.append(
                    {
                        "name": name,
                        "found": False,
                        "avg_cost": None,
                        "last_cost": None,
                        "samples": 0,
                    }
                )
                continue

            pr_result = await session.execute(
                select(PurchaseRecord)
                .where(PurchaseRecord.item_id == match.id)
                .where(PurchaseRecord.unit_cost.is_not(None))
                .order_by(PurchaseRecord.purchased_at.desc())
            )
            priced = pr_result.scalars().all()
            costs = [p.unit_cost for p in priced]
            out.append(
                {
                    "name": match.name.title(),
                    "found": True,
                    "avg_cost": round(mean(costs), 2) if costs else None,
                    "last_cost": costs[0] if costs else None,
                    "samples": len(costs),
                }
            )

    return {"items": out}


@mcp.tool()
async def consume_items(items: list[str], note: str | None = None) -> dict[str, Any]:
    """Mark items consumed, for example after a meal.

    Updates each item's inventory status. An item not yet tracked is
    created automatically — call this freely for any ingredient used,
    whether or not it has been tracked before.

    Args:
        items: Item names consumed (alias and fuzzy matched).
        note: Optional context, e.g. "chicken thigh tacos".
    """
    if not items:
        raise ToolError("items is empty; consume_items needs at least one item name.")

    async with _session_factory()() as session:
        consumed: list[str] = []
        created: list[str] = []
        touched: set[int] = set()
        now = datetime.now(UTC)

        for name in items:
            cleaned = name.strip()
            if not cleaned:
                raise ToolError("Every item needs a non-empty name.")
            match = await resolve_item(session, cleaned)
            if match is None:
                display = cleaned.lower()
                match = Item(
                    name=display, normalized=normalize(cleaned), first_seen=now, last_seen=now
                )
                session.add(match)
                await session.flush()
                created.append(match.name.title())
            session.add(
                ConsumptionEvent(item_id=match.id, note=note, occurred_at=now, source="agent")
            )
            consumed.append(match.name.title())
            touched.add(match.id)

        await session.commit()

        for item_id in touched:
            await inv_service.recalculate_inventory(session, item_id)

    return {"consumed": consumed, "created": created}


@mcp.tool()
async def set_preferred_store(item_name: str, store: str | None = None) -> dict[str, Any]:
    """Set or clear the store an item is usually bought at.

    This is a display hint only (e.g. milk -> "Aldi"). It does not affect
    purchase recording or depletion. Pass store to set or overwrite it;
    omit it, or pass null, to clear it.

    Args:
        item_name: The item to set the preference on (alias and fuzzy
            matched). Must already exist — this never creates an item.
        store: The preferred store name. Omit or pass null to clear it.
    """
    async with _session_factory()() as session:
        match = await resolve_item(session, item_name)
        if match is None:
            raise ToolError(f"Item '{item_name}' not found.")

        cleaned = store.strip() if store and store.strip() else None
        match.preferred_store = cleaned
        await session.commit()

    return {"item": match.name.title(), "preferred_store": cleaned, "cleared": cleaned is None}


@mcp.tool()
async def set_purchase_cost(item_name: str, purchased_at: str, cost: float) -> dict[str, Any]:
    """Attach a price to a purchase that already exists.

    Use this to backfill a cost from a receipt onto a purchase already on
    file, without creating a new one. Look up the item's purchase dates
    with get_item_history first. When the item has no purchase on this
    date, use record_purchase instead.

    Args:
        item_name: The purchased item (alias and fuzzy matched).
        purchased_at: Which purchase to price — an ISO timestamp or
            YYYY-MM-DD. Matched on the UTC day; there is at most one
            purchase per item per day.
        cost: The per-unit price paid.
    """
    target_day = _parse_target_day(purchased_at)

    async with _session_factory()() as session:
        match = await resolve_item(session, item_name)
        if match is None:
            raise ToolError(f"Item '{item_name}' not found.")

        target = await _find_purchase(session, match.id, target_day, match.name)

        target.unit_cost = cost
        await session.commit()

    return {
        "item": match.name.title(),
        "purchased_at": target.purchased_at.isoformat(),
        "cost": cost,
    }


@mcp.tool()
async def set_purchase_store(
    store: str,
    purchased_at: str,
    item_names: list[str] | None = None,
) -> dict[str, Any]:
    """Tag which store one or more existing purchases came from.

    A receipt is one store on one date, so the common case is one call for
    the whole trip: pass the date and omit item_names to tag every purchase
    on that date. Pass item_names to tag only those items' purchases.

    Args:
        store: The store name, e.g. "Aldi".
        purchased_at: The purchase date to tag — an ISO timestamp or
            YYYY-MM-DD. Matched on the UTC day.
        item_names: Items to limit the tag to (alias and fuzzy matched).
            Omit to tag every purchase on that date.
    """
    target_day = _parse_target_day(purchased_at)

    async with _session_factory()() as session:
        if item_names:
            updated: list[str] = []
            not_found: list[str] = []
            for name in item_names:
                match = await resolve_item(session, name)
                if match is None:
                    not_found.append(name)
                    continue
                result = await session.execute(
                    select(PurchaseRecord).where(
                        PurchaseRecord.item_id == match.id,
                        PurchaseRecord.purchase_date == target_day,
                    )
                )
                target = result.scalar_one_or_none()
                if target is None:
                    not_found.append(match.name.title())
                    continue
                target.store = store
                updated.append(match.name.title())
            await session.commit()
            return {
                "store": store,
                "date": target_day.isoformat(),
                "updated": updated,
                "not_found": not_found,
            }

        # No item filter: tag every purchase on that date.
        result = await session.execute(
            select(PurchaseRecord).where(PurchaseRecord.purchase_date == target_day)
        )
        targets = result.scalars().all()
        item_ids = {t.item_id for t in targets}
        for t in targets:
            t.store = store
        await session.commit()

        names: list[str] = []
        if item_ids:
            name_result = await session.execute(select(Item).where(Item.id.in_(item_ids)))
            names = sorted(i.name.title() for i in name_result.scalars().all())

    return {
        "store": store,
        "date": target_day.isoformat(),
        "updated_count": len(targets),
        "updated": names,
    }


@mcp.tool()
async def delete_purchase(item_name: str, purchased_at: str) -> dict[str, Any]:
    """Delete a single purchase record without removing the item.

    Use this to drop one bad or duplicate purchase (e.g. a line logged
    against the wrong date) while keeping the item and the rest of its
    history. Inventory status is recalculated from the purchases that
    remain. To remove the item and all its history, use delete_item.

    Args:
        item_name: The item whose purchase to remove (alias and fuzzy
            matched).
        purchased_at: Which purchase to delete — an ISO timestamp or
            YYYY-MM-DD. Matched on the UTC day; there is at most one
            purchase per item per day.
    """
    target_day = _parse_target_day(purchased_at)

    async with _session_factory()() as session:
        match = await resolve_item(session, item_name)
        if match is None:
            raise ToolError(f"Item '{item_name}' not found.")

        target = await _find_purchase(session, match.id, target_day, match.name)

        await session.delete(target)
        await session.commit()
        await inv_service.recalculate_inventory(session, match.id)

    return {"item": match.name.title(), "deleted_purchase_date": target_day.isoformat()}


async def _find_purchase(
    session: AsyncSession, item_id: int, target_day: date, item_name: str
) -> PurchaseRecord:
    """Return the one purchase for ``item_id`` on ``target_day``, or raise ``ToolError``."""
    result = await session.execute(
        select(PurchaseRecord).where(
            PurchaseRecord.item_id == item_id,
            PurchaseRecord.purchase_date == target_day,
        )
    )
    target = result.scalar_one_or_none()
    if target is not None:
        return target

    dates_result = await session.execute(
        select(PurchaseRecord.purchase_date)
        .where(PurchaseRecord.item_id == item_id)
        .order_by(PurchaseRecord.purchase_date.desc())
    )
    dates = dates_result.scalars().all()
    available = ", ".join(d.isoformat() for d in dates) or "none"
    raise ToolError(
        f"No purchase of '{item_name.title()}' on {target_day.isoformat()}. "
        f"Purchases exist on: {available}."
    )


@mcp.tool()
async def delete_item(item_name: str, confirm: bool = False) -> dict[str, Any]:
    """Permanently delete a tracked item and all of its history.

    Removes the item's purchase records, consumption events, aliases, and
    inventory state. This cannot be undone.

    Requires confirm=true to actually delete. When confirm is false (the
    default), the item is looked up and what would be removed is reported,
    without deleting anything.

    Args:
        item_name: The item to delete (alias and fuzzy matched).
        confirm: Must be true to delete. False (the default) is a dry run
            that reports the item and its purchase count.
    """
    async with _session_factory()() as session:
        match = await resolve_item(session, item_name)
        if match is None:
            raise ToolError(f"Item '{item_name}' not found.")

        if not confirm:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(PurchaseRecord)
                    .where(PurchaseRecord.item_id == match.id)
                )
            ).scalar_one()
            return {
                "confirm_required": True,
                "item": match.name.title(),
                "purchases_to_remove": count,
                "message": "Pass confirm=true to permanently delete this item and all its history.",
            }

        item_id = match.id
        item_display = match.name.title()
        purchases_removed = (
            await session.execute(
                select(func.count())
                .select_from(PurchaseRecord)
                .where(PurchaseRecord.item_id == item_id)
            )
        ).scalar_one()
        await session.execute(delete(PurchaseRecord).where(PurchaseRecord.item_id == item_id))
        await session.execute(delete(ConsumptionEvent).where(ConsumptionEvent.item_id == item_id))
        await session.execute(delete(Inventory).where(Inventory.item_id == item_id))
        await session.execute(delete(ItemAlias).where(ItemAlias.item_id == item_id))
        await session.execute(delete(Item).where(Item.id == item_id))
        await session.commit()

    return {"deleted": item_display, "purchases_removed": purchases_removed}


@mcp.tool()
async def add_alias(item_name: str, alias: str) -> dict[str, Any]:
    """Teach the pantry that one name refers to an existing item.

    Use this when a receipt says one thing but the pantry tracks it under
    another name — e.g. a receipt says "Trail Mix Bars" but the pantry
    tracks "Granola Bars". Do not alias genuinely different products:
    almond milk is not milk. When unsure, ask instead of aliasing.

    Args:
        item_name: The existing pantry item the alias refers to (alias and
            fuzzy matched). Must already exist — this never creates a new
            item.
        alias: The alternate name to add, e.g. "trail mix bars".
    """
    async with _session_factory()() as session:
        target = await resolve_item(session, item_name)
        if target is None:
            raise ToolError(f"Item '{item_name}' not found. add_alias needs an existing item.")

        norm = normalize(alias)
        if not norm:
            raise ToolError("alias is empty.")

        if norm == target.normalized:
            return {
                "item": target.name.title(),
                "alias": alias.strip().lower(),
                "already_aliased": True,
            }

        existing_item = (
            (await session.execute(select(Item).where(Item.normalized == norm))).scalars().first()
        )
        if existing_item is not None:
            raise ToolError(
                f"'{alias}' is already a tracked item "
                f"('{existing_item.name.title()}'), not an alias."
            )

        existing_alias = (
            (await session.execute(select(ItemAlias).where(ItemAlias.normalized == norm)))
            .scalars()
            .first()
        )
        if existing_alias is not None:
            if existing_alias.item_id == target.id:
                return {
                    "item": target.name.title(),
                    "alias": existing_alias.alias,
                    "already_aliased": True,
                }
            owner = await session.get(Item, existing_alias.item_id)
            owner_name = owner.name.title() if owner else "another item"
            raise ToolError(f"'{alias}' is already an alias of '{owner_name}'.")

        display = alias.strip().lower()
        session.add(ItemAlias(item_id=target.id, alias=display, normalized=norm, source="agent"))
        await session.commit()

    return {"item": target.name.title(), "alias": display, "already_aliased": False}


@mcp.tool()
async def list_aliases(item_name: str | None = None) -> dict[str, Any]:
    """List alternate names on file for one item, or for every item.

    Args:
        item_name: The item to list aliases for (alias and fuzzy matched).
            Omit to list every item that has at least one alias.
    """
    async with _session_factory()() as session:
        if item_name:
            match = await resolve_item(session, item_name)
            if match is None:
                raise ToolError(f"Item '{item_name}' not found.")
            alias_result = await session.execute(
                select(ItemAlias).where(ItemAlias.item_id == match.id).order_by(ItemAlias.alias)
            )
            aliases = alias_result.scalars().all()
            return {"item": match.name.title(), "aliases": [a.alias.title() for a in aliases]}

        aliases_map = await get_aliases_by_item(session)
        if not aliases_map:
            return {"items": []}

        items_result = await session.execute(
            select(Item).where(Item.id.in_(aliases_map.keys())).order_by(Item.name)
        )
        items = items_result.scalars().all()
        return {
            "items": [
                {"item": i.name.title(), "aliases": [a.title() for a in aliases_map[i.id]]}
                for i in items
            ]
        }


class BearerAuthMiddleware:
    """Require ``Authorization: Bearer <token>`` on every path except ``OPEN_PATHS``.

    Skips the check entirely when ``token`` is ``None`` (no ``ADDON_TOKEN``
    configured): the docker network is the boundary in that case.
    """

    def __init__(self, app: ASGIApp, token: str | None) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.token is None or scope["path"] in OPEN_PATHS:
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
    """Build the addon's ASGI app: the MCP routes plus the auth wrapper.

    ``host="0.0.0.0"`` turns off the SDK's loopback-only DNS-rebinding guard: a
    caller reaches this addon by its compose or cluster hostname (for example
    ``http://pantry:8000/mcp``), never by ``localhost``, and the guard would
    otherwise reject every real request.
    """
    inner = mcp.streamable_http_app(streamable_http_path="/mcp", host="0.0.0.0")
    token = os.environ.get("ADDON_TOKEN") or None
    return BearerAuthMiddleware(inner, token)
