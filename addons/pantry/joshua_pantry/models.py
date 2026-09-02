"""SQLAlchemy ORM models for the pantry data layer.

Ported from the joshua-pantry backend (``pantry/backend/app/models.py``).
This addon has no poller and no Apple Reminders link, so the poller-only
tables (``Snapshot``, ``SnapshotItem``, ``Event``) and the reminder id
(``Item.mag_id``) are dropped. ``PurchaseRecord`` gains ``sku``, ``upc``, and
``quantity`` for barcode- and receipt-driven purchases.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """A timezone-aware UTC ``datetime``, identical on both dialects.

    Postgres (asyncpg) returns a tz-aware ``datetime`` for
    ``DateTime(timezone=True)``. SQLite stores the same value as an ISO
    string but its driver hands back a naive ``datetime``, so every value
    here is UTC and every value that comes back naive is stamped ``UTC`` —
    the two dialects then behave identically to the rest of this package.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    # Every naive Python ``datetime`` column is really a UTC instant, so map
    # the type once here instead of repeating ``UTCDateTime()`` on every
    # column.
    type_annotation_map = {datetime: UTCDateTime()}


def _now() -> datetime:
    return datetime.now(UTC)


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    normalized: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)

    # DB-level ON DELETE SET NULL handles unassignment; don't load items on delete.
    items: Mapped[list[Item]] = relationship(back_populates="category", passive_deletes=True)


class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    normalized: Mapped[str] = mapped_column(Text)
    first_seen: Mapped[datetime] = mapped_column(default=_now)
    last_seen: Mapped[datetime | None] = mapped_column(default=None)
    last_purchased_at: Mapped[datetime | None] = mapped_column(default=None)
    is_tracked: Mapped[bool] = mapped_column(default=True)
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), index=True, default=None
    )
    # Preferred store this item is usually bought at (e.g. "Aldi"). Null = no
    # preference. Display/guidance hint only — does not affect purchase or
    # depletion logic.
    preferred_store: Mapped[str | None] = mapped_column(Text, default=None)
    # The exact product this item resolves to (e.g. "spaghetti sauce" -> one
    # UPC), when the household has a settled choice. Null is a first-class
    # answer meaning "ask the person" — never guess from products history.
    # ``use_alter`` breaks the items<->products create-table cycle (see
    # Product.item_id below): SQLAlchemy defers this constraint to a separate
    # ALTER TABLE after both tables exist, instead of erroring on the cycle.
    preferred_product_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "products.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_items_preferred_product_id",
        ),
        index=True,
        default=None,
    )
    # "auto" (confident) or "low" (tentative); null when preferred_product_id
    # is null. See set_preferred_product in server.py for how each is set.
    preference_confidence: Mapped[str | None] = mapped_column(Text, default=None)
    # "imported" | "manual" | "learned"; null when preferred_product_id is null.
    preference_source: Mapped[str | None] = mapped_column(Text, default=None)

    purchase_records: Mapped[list[PurchaseRecord]] = relationship(back_populates="item")
    inventory: Mapped[Inventory | None] = relationship(back_populates="item", uselist=False)
    consumption_events: Mapped[list[ConsumptionEvent]] = relationship(back_populates="item")
    aliases: Mapped[list[ItemAlias]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )
    category: Mapped[Category | None] = relationship(back_populates="items")
    products: Mapped[list[Product]] = relationship(
        back_populates="item",
        foreign_keys="Product.item_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    preferred_product: Mapped[Product | None] = relationship(foreign_keys=[preferred_product_id])


class ItemAlias(Base):
    __tablename__ = "item_aliases"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    alias: Mapped[str] = mapped_column(Text)
    # Globally unique: one alias name can only ever point to one item.
    normalized: Mapped[str] = mapped_column(Text, unique=True)
    source: Mapped[str] = mapped_column(Text, default="manual")  # manual | agent | merge
    created_at: Mapped[datetime] = mapped_column(default=_now)

    item: Mapped[Item] = relationship(back_populates="aliases")


class PurchaseRecord(Base):
    __tablename__ = "purchase_records"
    __table_args__ = (
        # At most one purchase per item per day. purchase_date is the UTC date
        # of purchased_at; the app records via upsert_purchase so same-day
        # entries (a barcode scan, a receipt, a manual edit) collapse into one
        # row instead of colliding with this constraint.
        UniqueConstraint("item_id", "purchase_date", name="uq_purchase_records_item_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    purchased_at: Mapped[datetime]
    purchase_date: Mapped[date]
    source: Mapped[str] = mapped_column(Text, default="automatic")
    unit_cost: Mapped[float | None] = mapped_column(default=None)
    store: Mapped[str | None] = mapped_column(Text, default=None)
    sku: Mapped[str | None] = mapped_column(Text, default=None)
    upc: Mapped[str | None] = mapped_column(Text, default=None)
    quantity: Mapped[float | None] = mapped_column(default=None)

    item: Mapped[Item] = relationship(back_populates="purchase_records")


class Product(Base):
    """One physical product a household can buy for an item.

    "Spaghetti Sauce" (the item) may resolve to several products over time —
    a jar of Rao's, a jar of the store brand — each its own row here, found
    by ``upc`` or by ``sku``+``store`` (see ``joshua_pantry.products``).
    ``extra`` holds retailer-specific ids (a future Instacart product id, for
    example) with no schema change: it is read and written whole, never
    queried into.
    """

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    # Unique when set; a bare UNIQUE constraint allows any number of NULL
    # rows on both SQLite and Postgres, so a product with no known UPC never
    # collides with another (see test_models.py for a same-item proof and
    # test_migrations.py for the pre-P2.3 upgrade path).
    upc: Mapped[str | None] = mapped_column(Text, unique=True, default=None)
    sku: Mapped[str | None] = mapped_column(Text, default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    store: Mapped[str | None] = mapped_column(Text, default=None)
    size: Mapped[str | None] = mapped_column(Text, default=None)  # e.g. "24 oz"
    unit: Mapped[str | None] = mapped_column(Text, default=None)
    # Retailer-specific ids and other future fields; read and written whole.
    extra: Mapped[dict | None] = mapped_column(JSON, default=None)
    last_purchased_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now)

    item: Mapped[Item] = relationship(back_populates="products", foreign_keys=[item_id])


class Inventory(Base):
    __tablename__ = "inventory"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), unique=True)
    last_purchased_at: Mapped[datetime | None] = mapped_column(default=None)
    avg_cycle_days: Mapped[float | None] = mapped_column(default=None)
    estimated_depletion: Mapped[datetime | None] = mapped_column(default=None)
    algo_version: Mapped[str] = mapped_column(Text, default="v1")
    updated_at: Mapped[datetime] = mapped_column(default=_now)

    item: Mapped[Item] = relationship(back_populates="inventory")


class ConsumptionEvent(Base):
    __tablename__ = "consumption_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    note: Mapped[str | None] = mapped_column(Text, default=None)
    occurred_at: Mapped[datetime] = mapped_column(default=_now)
    source: Mapped[str] = mapped_column(Text, default="agent")

    item: Mapped[Item] = relationship(back_populates="consumption_events")
