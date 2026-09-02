"""Product identity: matching a purchase line to one physical product.

An item ("spaghetti sauce") can be bought as several distinct products (a
jar of Rao's, a jar of the store brand). This module finds or creates the
``Product`` row a purchase line refers to, keyed on ``upc`` when the line has
one, or on ``sku``+``store`` otherwise. A ``upc`` already on file under a
different item is refused rather than silently moved — the same barcode
should never point at two different pantry items.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Item, Product


def product_dict(product: Product | None) -> dict[str, Any] | None:
    """The compact product shape every tool that surfaces a product returns.

    ``None`` in, ``None`` out — this is what a null preference looks like.
    """
    if product is None:
        return None
    return {
        "upc": product.upc,
        "sku": product.sku,
        "description": product.description,
        "size": product.size,
        "unit": product.unit,
        "store": product.store,
        "last_purchased_at": (
            product.last_purchased_at.isoformat() if product.last_purchased_at else None
        ),
    }


async def find_or_create_product(
    session: AsyncSession,
    item: Item,
    *,
    when: datetime,
    upc: str | None = None,
    sku: str | None = None,
    store: str | None = None,
    description: str | None = None,
    size: str | None = None,
    unit: str | None = None,
) -> Product | None:
    """Find or create the ``Product`` a purchase line refers to, and stamp it seen.

    Matches on ``upc`` first when given; otherwise on ``sku``+``store`` for
    this item, since a bare sku can repeat across stores. Returns ``None``
    when the line carries neither — nothing to link a product on.

    A field left blank on an existing product (``description``, ``size``,
    ``unit``, ``store``) is filled from this call, but a value already on
    file is never overwritten — the same first-write-wins rule
    ``purchases.upsert_purchase`` uses for sku/upc/quantity.

    Raises ``ToolError`` when ``upc`` already names a product under a
    different item: a barcode belongs to one item, never two.
    """
    if not upc and not (sku and store):
        return None

    product: Product | None = None

    if upc:
        result = await session.execute(select(Product).where(Product.upc == upc))
        product = result.scalar_one_or_none()
        if product is not None and product.item_id != item.id:
            other = await session.get(Item, product.item_id)
            other_name = other.name.title() if other else "another item"
            raise ToolError(
                f"UPC '{upc}' is already on file for '{other_name}', "
                f"not '{item.name.title()}'. A UPC belongs to one item — "
                "fix the receipt line, or use add_alias if the item names "
                "should be merged."
            )

    if product is None and sku and store:
        result = await session.execute(
            select(Product).where(
                Product.item_id == item.id, Product.sku == sku, Product.store == store
            )
        )
        product = result.scalar_one_or_none()

    if product is None:
        product = Product(
            item_id=item.id,
            upc=upc,
            sku=sku,
            store=store,
            description=description,
            size=size,
            unit=unit,
            last_purchased_at=when,
        )
        session.add(product)
        await session.flush()
        return product

    product.last_purchased_at = when
    if upc and not product.upc:
        product.upc = upc
    if sku and not product.sku:
        product.sku = sku
    if store and not product.store:
        product.store = store
    if description and not product.description:
        product.description = description
    if size and not product.size:
        product.size = size
    if unit and not product.unit:
        product.unit = unit
    return product


async def find_or_create_preference_product(
    session: AsyncSession,
    item: Item,
    *,
    upc: str | None = None,
    sku: str | None = None,
    store: str | None = None,
    description: str | None = None,
    size: str | None = None,
    unit: str | None = None,
) -> Product:
    """Find or create the ``Product`` an imported preference names.

    Used by ``import_data`` (P2.4) for an item's ``preference`` block, where
    a bare ``sku`` names a settled choice on its own. This differs from
    ``find_or_create_product``, which needs ``sku`` and ``store`` together
    because a bare sku can repeat across stores on a purchase line — a
    preference names one product, so a bare sku is enough here. Raises
    ``ToolError`` when ``upc`` already names a product under a different
    item, the same rule ``find_or_create_product`` enforces. The caller
    guarantees ``upc`` or ``sku`` is set.
    """
    product: Product | None = None

    if upc:
        result = await session.execute(select(Product).where(Product.upc == upc))
        product = result.scalar_one_or_none()
        if product is not None and product.item_id != item.id:
            other = await session.get(Item, product.item_id)
            other_name = other.name.title() if other else "another item"
            raise ToolError(
                f"UPC '{upc}' is already on file for '{other_name}', "
                f"not '{item.name.title()}'. A UPC belongs to one item."
            )

    if product is None and sku:
        query = select(Product).where(Product.item_id == item.id, Product.sku == sku)
        if store:
            query = query.where(Product.store == store)
        product = (await session.execute(query)).scalars().first()

    if product is None:
        product = Product(
            item_id=item.id,
            upc=upc,
            sku=sku,
            store=store,
            description=description,
            size=size,
            unit=unit,
        )
        session.add(product)
        await session.flush()
        return product

    if upc and not product.upc:
        product.upc = upc
    if sku and not product.sku:
        product.sku = sku
    if store and not product.store:
        product.store = store
    if description and not product.description:
        product.description = description
    if size and not product.size:
        product.size = size
    if unit and not product.unit:
        product.unit = unit
    return product
