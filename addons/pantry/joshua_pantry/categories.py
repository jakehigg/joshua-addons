"""Category listing for the REST API.

Ported from the joshua-pantry backend
(``pantry/backend/app/services/categories.py``), trimmed to what
``joshua_pantry.api`` needs: the alphabetical list with each category's
tracked-item count. ``resolution.resolve_or_create_category`` already covers
lookup and creation; nothing in this addon renames or deletes a category yet.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Category, Item


async def list_categories(session: AsyncSession) -> list[dict]:
    """All categories with their tracked-item counts, alphabetical."""
    result = await session.execute(
        select(Category, func.count(Item.id))
        .outerjoin(Item, (Item.category_id == Category.id) & Item.is_tracked.is_(True))
        .group_by(Category.id)
        .order_by(Category.name)
    )
    return [{"id": cat.id, "name": cat.name, "item_count": count} for cat, count in result.all()]
