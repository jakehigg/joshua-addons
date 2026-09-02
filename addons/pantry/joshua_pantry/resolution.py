"""Single source of truth for item name normalization and lookup.

Every code path that turns a free-text name (a receipt line, a barcode scan,
a manual entry) into an Item row goes through resolve_item so that aliases
and the conservative fuzzy rule behave identically everywhere.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Category, Item, ItemAlias

_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")


def normalize(name: str) -> str:
    """Lowercase, replace punctuation with spaces, collapse whitespace.

    'Non-Fat Greek Yogurt' and 'non fat greek yogurt' normalize identically.
    """
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", name.lower())).strip()


async def resolve_item(
    session: AsyncSession,
    name: str,
    *,
    fuzzy: bool = True,
) -> Item | None:
    """Resolve a free-text name to an existing Item, or None.

    Order: exact item name (including untracked, so hidden items are reused
    rather than duplicated) → exact alias → optional conservative fuzzy match
    over tracked items and their aliases. Fuzzy only allows substring
    containment when lengths are within 85% (banana/bananas — never
    milk/almond milk), so distinct products are never auto-conflated.
    """
    norm = normalize(name)
    if not norm:
        return None

    result = await session.execute(select(Item).where(Item.normalized == norm))
    item = result.scalars().first()
    if item is not None:
        return item

    result = await session.execute(
        select(Item)
        .join(ItemAlias, ItemAlias.item_id == Item.id)
        .where(ItemAlias.normalized == norm)
    )
    item = result.scalars().first()
    if item is not None:
        return item

    if not fuzzy:
        return None

    result = await session.execute(select(Item).where(Item.is_tracked.is_(True)))
    candidates: list[tuple[str, Item]] = [(i.normalized, i) for i in result.scalars().all()]
    result = await session.execute(
        select(ItemAlias, Item)
        .join(Item, ItemAlias.item_id == Item.id)
        .where(Item.is_tracked.is_(True))
    )
    candidates.extend((a.normalized, i) for a, i in result.all())

    for cand_norm, cand_item in candidates:
        shorter = min(len(norm), len(cand_norm))
        longer = max(len(norm), len(cand_norm))
        if longer > 0 and shorter / longer >= 0.85:
            if norm in cand_norm or cand_norm in norm:
                return cand_item
    return None


async def resolve_or_create_category(session: AsyncSession, name: str) -> Category:
    """Find or create a ``Category`` by its normalized name.

    Mirrors item name normalization, so "Dairy" and "dairy" collapse to one
    row. Used by ``import_data`` (P2.4), the first write path onto this
    table.
    """
    norm = normalize(name)
    result = await session.execute(select(Category).where(Category.normalized == norm))
    category = result.scalars().first()
    if category is not None:
        return category
    category = Category(name=name.strip().lower(), normalized=norm)
    session.add(category)
    await session.flush()
    return category


async def get_aliases_by_item(session: AsyncSession) -> dict[int, list[str]]:
    """All aliases grouped by item_id, in one query."""
    result = await session.execute(select(ItemAlias).order_by(ItemAlias.alias))
    grouped: dict[int, list[str]] = {}
    for a in result.scalars().all():
        grouped.setdefault(a.item_id, []).append(a.alias)
    return grouped
