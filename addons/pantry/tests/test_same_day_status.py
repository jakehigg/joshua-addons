"""A purchase from a bare receipt date and a consumption on the same day (issue #12).

``record_purchase`` stores a bare date at 00:00 UTC. ``consume_items`` stores
the current time. A purchase on the date of the latest consumption, or later,
must win. A consumption on a later date than the purchase must still win.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from joshua_pantry import inventory, server
from joshua_pantry.models import Inventory

# Day D is yesterday in UTC, so a fresh purchase on day D is recent enough to
# be in stock without a purchase cycle.
_DAY_D = (datetime.now(UTC) - timedelta(days=1)).date()
_NOON_D = datetime(_DAY_D.year, _DAY_D.month, _DAY_D.day, 12, tzinfo=UTC)


def _freeze_server_clock(monkeypatch: pytest.MonkeyPatch, instant: datetime) -> None:
    """Make ``datetime.now`` in ``joshua_pantry.server`` return ``instant``."""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return instant

    monkeypatch.setattr(server, "datetime", _Frozen)


async def _api_status(app_factory, name: str) -> str:
    app = app_factory(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/inventory")
    assert response.status_code == 200
    return {row["name"]: row["status"] for row in response.json()}[name]


async def test_purchase_on_the_day_of_a_consumption_is_in_stock(
    bound_db, app_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Consume at 12:00 on day D, then record a purchase dated day D: the item is in stock."""
    _freeze_server_clock(monkeypatch, _NOON_D)
    await server.consume_items(["mushrooms"])
    await server.record_purchase(
        items=[server.PurchaseLine(name="mushrooms", cost=2.0)],
        purchased_at=_DAY_D.isoformat(),
    )
    monkeypatch.undo()

    rows = await server.get_inventory()
    assert [(r["name"], r["status"]) for r in rows] == [("Mushrooms", "in_stock")]
    assert rows[0]["estimated_depletion"] is None

    history = await server.get_item_history("mushrooms")
    assert history["status"] == "in_stock"

    assert await _api_status(app_factory, "Mushrooms") == "in_stock"


async def test_consumption_on_a_later_day_than_the_purchase_is_out_of_stock(
    bound_db, app_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Record a purchase dated the day before D, then consume at 12:00 on day D: out of stock."""
    await server.record_purchase(
        items=[server.PurchaseLine(name="spinach", cost=3.0)],
        purchased_at=(_DAY_D - timedelta(days=1)).isoformat(),
    )
    _freeze_server_clock(monkeypatch, _NOON_D)
    await server.consume_items(["spinach"])
    monkeypatch.undo()

    rows = await server.get_inventory()
    assert [(r["name"], r["status"]) for r in rows] == [("Spinach", "out_of_stock")]

    history = await server.get_item_history("spinach")
    assert history["status"] == "out_of_stock"

    assert await _api_status(app_factory, "Spinach") == "out_of_stock"


def test_timed_purchase_still_compares_by_time() -> None:
    """A purchase with a real time loses to a later consumption on the same day."""
    bought = _NOON_D - timedelta(hours=2)
    inv = Inventory(item_id=1, last_purchased_at=bought)
    assert inventory.get_item_status(inv, last_consumed_at=_NOON_D) == "out_of_stock"
    assert inventory.consumed_after_purchase(_NOON_D, bought) is True
    assert inventory.consumed_after_purchase(bought - timedelta(hours=1), bought) is False


def test_bare_date_purchase_compares_by_date() -> None:
    midnight_d = _NOON_D.replace(hour=0)
    assert inventory.consumed_after_purchase(_NOON_D, midnight_d) is False
    assert inventory.consumed_after_purchase(_NOON_D + timedelta(days=1), midnight_d) is True
    assert inventory.consumed_after_purchase(_NOON_D, None) is True
