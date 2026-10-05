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

# Words that do not say which food a name is. find_possible_matches ignores them.
FILLER_WORDS = frozenset(
    {
        # quality and freshness
        "organic", "fresh", "natural", "premium", "classic", "original", "plain",
        "value", "select", "choice", "fancy", "grade", "pure", "real", "simply",
        # size words
        "large", "small", "medium", "mini", "big", "little", "jumbo", "extra",
        "family", "size", "giant", "bulk", "xl",
        # units and packs
        "oz", "fl", "lb", "lbs", "g", "kg", "mg", "ml", "l", "ltr", "gal", "qt", "pt",
        "ct", "count", "pk", "pack", "ea", "each", "dozen", "doz", "bag", "box", "can",
        "jar", "bottle", "bunch", "pkg",
        # joining words
        "a", "an", "and", "the", "of", "with", "in", "for",
    }
)  # fmt: skip
# A number, or a number with a unit stuck to it: "12", "1.5", "16oz", "2lb".
_SIZE_TOKEN_RE = re.compile(r"^\d+[a-z]*$")


def normalize(name: str) -> str:
    """Lowercase, replace punctuation with spaces, collapse whitespace.

    'Non-Fat Greek Yogurt' and 'non fat greek yogurt' normalize identically.
    """
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", name.lower())).strip()


def match_kind(query: str, name: str, aliases: list[str]) -> str | None:
    """Say how a free-text query matches one item, or return None.

    Returns "name" for an exact name, "alias" for an exact alias, and
    "words" when the name or an alias holds every word of the query. All
    three compare normalized text. "spinach" matches "baby spinach" by
    words, but "baby spinach" does not match "spinach".
    """
    query_norm = normalize(query)
    if not query_norm:
        return None
    alias_norms = [normalize(a) for a in aliases]
    if normalize(name) == query_norm:
        return "name"
    if query_norm in alias_norms:
        return "alias"
    words = set(query_norm.split())
    for candidate in [normalize(name), *alias_norms]:
        if words <= set(candidate.split()):
            return "words"
    return None


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


def _singular(word: str) -> str:
    """Remove a plural ending, so "potatoes" and "potato" compare equal."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith("oes"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def significant_words(name: str) -> set[str]:
    """The words of a name that say which food it is.

    Drops ``FILLER_WORDS`` and size tokens ("12", "16oz"), and removes plural
    endings. "Organic Little Yellow Potatoes 24oz" gives {"yellow", "potato"}.
    """
    return {
        _singular(word)
        for word in normalize(name).split()
        if word not in FILLER_WORDS and not _SIZE_TOKEN_RE.match(word)
    }


async def find_possible_matches(
    session: AsyncSession,
    name: str,
    *,
    exclude_ids: set[int] | frozenset[int] = frozenset(),
    limit: int = 3,
) -> list[dict]:
    """Tracked items that share one or more significant words with ``name``.

    This only suggests. It never merges or aliases, and ``resolve_item``
    stays the only automatic match. Each tracked item and each of its
    aliases count as a name of that item. Items with more shared words come
    first, then items in name order. Returns at most ``limit`` entries, each
    ``{"item": <display name>, "shared_words": [...]}``.
    """
    words = significant_words(name)
    if not words:
        return []

    result = await session.execute(select(Item).where(Item.is_tracked.is_(True)))
    names_by_item: dict[int, tuple[Item, list[str]]] = {
        i.id: (i, [i.name]) for i in result.scalars().all() if i.id not in exclude_ids
    }
    result = await session.execute(
        select(ItemAlias).where(ItemAlias.item_id.in_(names_by_item.keys()))
    )
    for alias in result.scalars().all():
        names_by_item[alias.item_id][1].append(alias.alias)

    scored: list[tuple[int, str, list[str]]] = []
    for item, item_names in names_by_item.values():
        shared: set[str] = set()
        for item_name in item_names:
            shared |= words & significant_words(item_name)
        if shared:
            scored.append((len(shared), item.name, sorted(shared)))

    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    return [{"item": n.title(), "shared_words": w} for _, n, w in scored[:limit]]


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
