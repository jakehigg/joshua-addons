"""The REST API behind the ported pantry web UI.

Mounted at ``/api`` on the same Starlette app that serves ``/mcp`` and
``/healthz`` (see ``server.build_app``). Every route here is open -- no
bearer token -- the same posture the old family UI had: a deployer gates
access with the ingress or the docker network, not this addon.

Every handler opens its own session and calls the same service functions the
MCP tools use (``inventory``, ``purchases``, ``resolution``, ``categories``,
``merge``), so a purchase edited from the UI and a purchase edited from an
MCP tool run the identical inventory-recalculation and alias-resolution
logic. ``joshua_pantry.server`` is imported for its module-level session
factory only; importing it here (rather than the reverse) keeps the two
modules free of an import cycle even though ``server.build_app`` mounts this
module's app.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import joshua_pantry.server as server
from joshua_pantry import categories as cat_service
from joshua_pantry import inventory as inv_service
from joshua_pantry import merge as merge_service
from joshua_pantry import purchases as purchases_service
from joshua_pantry.models import ConsumptionEvent, Item, ItemAlias
from joshua_pantry.resolution import normalize, resolve_item


def _detail(status_code: int, detail: str) -> JSONResponse:
    """An error body shaped ``{"detail": ...}`` -- what the ported frontend reads."""
    return JSONResponse({"detail": detail}, status_code=status_code)


def _int_param(
    raw: str | None, default: int, *, minimum: int, maximum: int | None = None
) -> int | None:
    """Parse a query parameter as an int within bounds. ``None`` means invalid."""
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return None
    if value < minimum or (maximum is not None and value > maximum):
        return None
    return value


async def _parse_json_object(request: Request) -> dict | None:
    """The request body as a JSON object, or ``None`` when it is not one."""
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


async def get_inventory(request: Request) -> JSONResponse:
    status_filter = request.query_params.get("status")
    async with server._session_factory()() as session:
        rows = await inv_service.get_all_inventory_status(session, status_filter)
    return JSONResponse(rows)


async def get_analytics(request: Request) -> JSONResponse:
    async with server._session_factory()() as session:
        rows = await inv_service.get_purchase_analytics(session)
    return JSONResponse(rows)


async def list_purchases(request: Request) -> JSONResponse:
    limit = _int_param(request.query_params.get("limit"), 100, minimum=1, maximum=500)
    offset = _int_param(request.query_params.get("offset"), 0, minimum=0)
    if limit is None:
        return _detail(400, "limit must be between 1 and 500")
    if offset is None:
        return _detail(400, "offset must be 0 or more")

    async with server._session_factory()() as session:
        rows, total = await purchases_service.list_purchases(session, limit=limit, offset=offset)
    return JSONResponse({"purchases": rows, "total": total, "limit": limit, "offset": offset})


_PURCHASE_EDIT_FIELDS = frozenset({"unit_cost", "store"})


async def update_purchase(request: Request) -> JSONResponse:
    purchase_id = request.path_params["purchase_id"]
    body = await _parse_json_object(request)
    if body is None:
        return _detail(400, "Invalid JSON body")
    fields = {k: v for k, v in body.items() if k in _PURCHASE_EDIT_FIELDS}
    if not fields:
        return _detail(400, "No fields to update")

    async with server._session_factory()() as session:
        updated = await purchases_service.update_purchase(session, purchase_id, fields)
    if updated is None:
        return _detail(404, "Purchase not found")
    return JSONResponse(updated)


async def delete_purchase(request: Request) -> JSONResponse:
    purchase_id = request.path_params["purchase_id"]
    async with server._session_factory()() as session:
        deleted = await purchases_service.delete_purchase(session, purchase_id)
    if not deleted:
        return _detail(404, "Purchase not found")
    return JSONResponse({"ok": True})


async def list_categories(request: Request) -> JSONResponse:
    async with server._session_factory()() as session:
        cats = await cat_service.list_categories(session)
    return JSONResponse(
        [{"id": c["id"], "name": c["name"].title(), "item_count": c["item_count"]} for c in cats]
    )


async def list_item_aliases(request: Request) -> JSONResponse:
    item_id = request.path_params["item_id"]
    async with server._session_factory()() as session:
        result = await session.execute(
            select(ItemAlias).where(ItemAlias.item_id == item_id).order_by(ItemAlias.alias)
        )
        aliases = result.scalars().all()
    return JSONResponse([{"id": a.id, "alias": a.alias} for a in aliases])


async def add_item_alias(request: Request) -> JSONResponse:
    item_id = request.path_params["item_id"]
    body = await _parse_json_object(request)
    alias = body.get("alias") if body else None
    if not isinstance(alias, str) or not alias.strip():
        return _detail(400, "alias is required")

    async with server._session_factory()() as session:
        item = await session.get(Item, item_id)
        if item is None:
            return _detail(404, "Item not found")
        try:
            outcome = await merge_service.add_alias(session, item, alias, merge_duplicates=False)
        except ValueError as exc:
            return _detail(400, str(exc))
        except merge_service.DuplicateItemError as exc:
            return _detail(
                409,
                f"'{alias}' is already a tracked item ('{exc.item.name.title()}', id "
                f"{exc.item.id}). Use merge to combine them.",
            )
        except merge_service.AliasConflictError as exc:
            return _detail(409, f"'{alias}' is already an alias of '{exc.owner.name.title()}'.")

        if outcome["alias_added"] is None:
            return _detail(409, f"'{alias}' already resolves to this item.")

        result = await session.execute(
            select(ItemAlias).where(
                ItemAlias.item_id == item_id, ItemAlias.alias == outcome["alias_added"]
            )
        )
        row = result.scalars().first()
    return JSONResponse({"id": row.id, "alias": row.alias})


async def remove_item_alias(request: Request) -> JSONResponse:
    item_id = request.path_params["item_id"]
    alias_id = request.path_params["alias_id"]
    async with server._session_factory()() as session:
        result = await session.execute(
            select(ItemAlias).where(ItemAlias.id == alias_id, ItemAlias.item_id == item_id)
        )
        alias = result.scalars().first()
        if alias is None:
            return _detail(404, "Alias not found")
        await session.delete(alias)
        await session.commit()
    return JSONResponse({"ok": True})


async def merge_items_route(request: Request) -> JSONResponse:
    source_id = request.path_params["source_id"]
    target_id = request.path_params["target_id"]
    if source_id == target_id:
        return _detail(400, "Cannot merge an item into itself")

    async with server._session_factory()() as session:
        source = await session.get(Item, source_id)
        target = await session.get(Item, target_id)
        if source is None or target is None:
            return _detail(404, "Item not found")
        result = await merge_service.merge_items(session, source, target)
    return JSONResponse(
        {
            "target_id": result["target_id"],
            "target_name": result["target_name"].title(),
            "alias_added": result["alias_added"],
            "moved_purchases": result["moved_purchases"],
        }
    )


async def rename_item_route(request: Request) -> JSONResponse:
    item_id = request.path_params["item_id"]
    body = await _parse_json_object(request)
    new_name = body.get("new_name") if body else None
    if not isinstance(new_name, str) or not new_name.strip():
        return _detail(400, "new_name is required")

    async with server._session_factory()() as session:
        item = await session.get(Item, item_id)
        if item is None:
            return _detail(404, "Item not found")
        try:
            item = await merge_service.rename_item(session, item, new_name)
        except ValueError as exc:
            return _detail(400, str(exc))
        except merge_service.AliasConflictError as exc:
            return _detail(409, f"'{new_name}' is already an alias of '{exc.owner.name.title()}'.")
    return JSONResponse({"id": item.id, "name": item.name.title()})


async def deactivate_item(request: Request) -> JSONResponse:
    item_id = request.path_params["item_id"]
    async with server._session_factory()() as session:
        item = await session.get(Item, item_id)
        if item is None:
            return _detail(404, "Item not found")
        item.is_tracked = False
        await session.commit()
    return JSONResponse({"ok": True})


async def mark_out_of_stock(request: Request) -> JSONResponse:
    item_id = request.path_params["item_id"]
    async with server._session_factory()() as session:
        item = await session.get(Item, item_id)
        if item is None:
            return _detail(404, "Item not found")
        session.add(ConsumptionEvent(item_id=item_id, source="manual"))
        await session.commit()
        await inv_service.recalculate_inventory(session, item_id)
        name = item.name
    return JSONResponse({"item_name": name.title()})


async def record_purchase_route(request: Request) -> JSONResponse:
    body = await _parse_json_object(request)
    name = body.get("name") if body else None
    if not isinstance(name, str) or not name.strip():
        return _detail(400, "name is required")

    now = datetime.now(UTC)
    async with server._session_factory()() as session:
        match = await resolve_item(session, name)
        created = False
        if match is None:
            match = Item(
                name=name.strip().lower(),
                normalized=normalize(name),
                first_seen=now,
                last_seen=now,
                is_tracked=True,
            )
            session.add(match)
            await session.flush()
            created = True
        else:
            match.is_tracked = True
            match.last_seen = now

        await purchases_service.upsert_purchase(session, match.id, now, source="manual")
        match.last_purchased_at = now
        await session.commit()
        await inv_service.recalculate_inventory(session, match.id)
        item_name = match.name
    return JSONResponse({"item_name": item_name.title(), "created": created})


def _routes() -> list[Route]:
    return [
        Route("/inventory", get_inventory, methods=["GET"]),
        Route("/inventory/record-purchase", record_purchase_route, methods=["POST"]),
        Route("/inventory/{item_id:int}/mark-out-of-stock", mark_out_of_stock, methods=["POST"]),
        Route("/analytics", get_analytics, methods=["GET"]),
        Route("/purchases", list_purchases, methods=["GET"]),
        Route("/purchases/{purchase_id:int}", update_purchase, methods=["PATCH"]),
        Route("/purchases/{purchase_id:int}", delete_purchase, methods=["DELETE"]),
        Route("/categories", list_categories, methods=["GET"]),
        Route("/items/{item_id:int}/aliases", list_item_aliases, methods=["GET"]),
        Route("/items/{item_id:int}/aliases", add_item_alias, methods=["POST"]),
        Route("/items/{item_id:int}/aliases/{alias_id:int}", remove_item_alias, methods=["DELETE"]),
        Route(
            "/items/{source_id:int}/merge-into/{target_id:int}",
            merge_items_route,
            methods=["POST"],
        ),
        Route("/items/{item_id:int}/rename", rename_item_route, methods=["POST"]),
        Route("/items/{item_id:int}", deactivate_item, methods=["DELETE"]),
    ]


def build_api_app() -> Starlette:
    """Build the ``/api`` sub-app. No auth: see the module docstring."""
    return Starlette(routes=_routes())
