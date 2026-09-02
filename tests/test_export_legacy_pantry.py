"""Tests for scripts/export_legacy_pantry.py: the legacy pantry exporter.

Offline: the mapping functions (rows in, ``items[]``/``purchases[]``/
``consumptions[]`` out), the alias-vs-item-name pre-check, stable ordering,
and the SELECT-only proof over the module's own source.

Integration (``@pytest.mark.integration``, needs ``LEGACY_PANTRY_DATABASE_URL``
pointed at a throwaway Postgres holding the OLD schema — never a live
instance): seed synthetic rows, run the exporter, then feed the result
through the real P2.4 ``import_data`` path into a fresh SQLite pantry and
check what landed.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from scripts import export_legacy_pantry as export

LEGACY_DB_ENV = "LEGACY_PANTRY_DATABASE_URL"

SCHEMA_SQL = """
CREATE TABLE categories (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    normalized TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE items (
    id SERIAL PRIMARY KEY,
    mag_id TEXT UNIQUE,
    name TEXT NOT NULL,
    normalized TEXT NOT NULL,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen TIMESTAMPTZ,
    last_purchased_at TIMESTAMPTZ,
    is_tracked BOOLEAN NOT NULL DEFAULT true,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    preferred_store TEXT
);

CREATE TABLE item_aliases (
    id SERIAL PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    normalized TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE purchase_records (
    id SERIAL PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id),
    purchased_at TIMESTAMPTZ NOT NULL,
    purchase_date DATE NOT NULL,
    source TEXT NOT NULL DEFAULT 'automatic',
    unit_cost DOUBLE PRECISION,
    store TEXT,
    CONSTRAINT uq_purchase_records_item_date UNIQUE (item_id, purchase_date)
);

CREATE TABLE consumption_events (
    id SERIAL PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id),
    note TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source TEXT NOT NULL DEFAULT 'agent'
);
"""


# --- offline: build_items ----------------------------------------------------


def test_build_items_maps_akas_category_and_store() -> None:
    items = [
        {"id": 1, "name": "milk", "category_id": 10, "preferred_store": "aldi", "is_tracked": True}
    ]
    categories = [{"id": 10, "name": "dairy"}]
    aliases = [{"item_id": 1, "alias": "whole milk"}]

    result, untracked = export.build_items(items, categories, aliases)

    assert result == [
        {"name": "milk", "akas": ["whole milk"], "category": "dairy", "preferred_store": "aldi"}
    ]
    assert untracked == 0


def test_build_items_omits_empty_fields() -> None:
    items = [
        {"id": 1, "name": "bread", "category_id": None, "preferred_store": None, "is_tracked": True}
    ]
    result, _ = export.build_items(items, [], [])
    assert result == [{"name": "bread"}]


def test_build_items_skips_untracked_and_counts_them() -> None:
    items = [
        {"id": 1, "name": "milk", "category_id": None, "preferred_store": None, "is_tracked": True},
        {
            "id": 2,
            "name": "old thing",
            "category_id": None,
            "preferred_store": None,
            "is_tracked": False,
        },
    ]
    result, untracked = export.build_items(items, [], [])
    assert [e["name"] for e in result] == ["milk"]
    assert untracked == 1


def test_build_items_sorted_by_name() -> None:
    items = [
        {"id": 1, "name": "rice", "category_id": None, "preferred_store": None, "is_tracked": True},
        {
            "id": 2,
            "name": "bread",
            "category_id": None,
            "preferred_store": None,
            "is_tracked": True,
        },
        {"id": 3, "name": "milk", "category_id": None, "preferred_store": None, "is_tracked": True},
    ]
    result, _ = export.build_items(items, [], [])
    assert [e["name"] for e in result] == ["bread", "milk", "rice"]


def test_build_items_multiple_akas_are_sorted() -> None:
    items = [
        {"id": 1, "name": "eggs", "category_id": None, "preferred_store": None, "is_tracked": True}
    ]
    aliases = [{"item_id": 1, "alias": "brown eggs"}, {"item_id": 1, "alias": "large eggs"}]
    result, _ = export.build_items(items, [], aliases)
    assert result[0]["akas"] == ["brown eggs", "large eggs"]


# --- offline: build_purchases -------------------------------------------------


def test_build_purchases_maps_and_omits_null_fields() -> None:
    tracked = {1: "milk"}
    purchases = [
        {
            "id": 1,
            "item_id": 1,
            "purchase_date": date(2026, 1, 5),
            "store": "aldi",
            "unit_cost": 3.5,
        },
        {
            "id": 2,
            "item_id": 1,
            "purchase_date": date(2026, 1, 12),
            "store": None,
            "unit_cost": None,
        },
    ]
    result = export.build_purchases(purchases, tracked)
    assert result == [
        {"item": "milk", "purchased_at": "2026-01-05", "store": "aldi", "cost": 3.5},
        {"item": "milk", "purchased_at": "2026-01-12"},
    ]


def test_build_purchases_excludes_untracked_items() -> None:
    tracked = {1: "milk"}
    purchases = [
        {
            "id": 1,
            "item_id": 1,
            "purchase_date": date(2026, 1, 5),
            "store": None,
            "unit_cost": None,
        },
        {
            "id": 2,
            "item_id": 2,
            "purchase_date": date(2026, 1, 5),
            "store": None,
            "unit_cost": None,
        },
    ]
    result = export.build_purchases(purchases, tracked)
    assert len(result) == 1
    assert result[0]["item"] == "milk"


def test_build_purchases_sorted_by_item_then_date() -> None:
    tracked = {1: "rice", 2: "bread"}
    purchases = [
        {
            "id": 1,
            "item_id": 1,
            "purchase_date": date(2026, 2, 1),
            "store": None,
            "unit_cost": None,
        },
        {
            "id": 2,
            "item_id": 2,
            "purchase_date": date(2026, 1, 1),
            "store": None,
            "unit_cost": None,
        },
        {
            "id": 3,
            "item_id": 1,
            "purchase_date": date(2026, 1, 1),
            "store": None,
            "unit_cost": None,
        },
    ]
    result = export.build_purchases(purchases, tracked)
    assert [(r["item"], r["purchased_at"]) for r in result] == [
        ("bread", "2026-01-01"),
        ("rice", "2026-01-01"),
        ("rice", "2026-02-01"),
    ]


# --- offline: build_consumptions ---------------------------------------------


def test_build_consumptions_maps_utc_iso_and_omits_null_note() -> None:
    tracked = {1: "milk"}
    consumptions = [
        {
            "id": 1,
            "item_id": 1,
            "occurred_at": datetime(2026, 1, 10, 8, 0, tzinfo=UTC),
            "note": "cereal",
        },
        {
            "id": 2,
            "item_id": 1,
            "occurred_at": datetime(2026, 1, 11, 8, 0, tzinfo=UTC),
            "note": None,
        },
    ]
    result = export.build_consumptions(consumptions, tracked)
    assert result == [
        {"item": "milk", "occurred_at": "2026-01-10T08:00:00+00:00", "note": "cereal"},
        {"item": "milk", "occurred_at": "2026-01-11T08:00:00+00:00"},
    ]


def test_build_consumptions_excludes_untracked_items() -> None:
    tracked = {1: "milk"}
    consumptions = [
        {"id": 1, "item_id": 2, "occurred_at": datetime(2026, 1, 10, tzinfo=UTC), "note": None},
    ]
    assert export.build_consumptions(consumptions, tracked) == []


def test_build_consumptions_sorted_by_item_then_timestamp() -> None:
    tracked = {1: "rice", 2: "bread"}
    consumptions = [
        {"id": 1, "item_id": 1, "occurred_at": datetime(2026, 1, 2, tzinfo=UTC), "note": None},
        {"id": 2, "item_id": 2, "occurred_at": datetime(2026, 1, 1, tzinfo=UTC), "note": None},
        {"id": 3, "item_id": 1, "occurred_at": datetime(2026, 1, 1, tzinfo=UTC), "note": None},
    ]
    result = export.build_consumptions(consumptions, tracked)
    assert [(r["item"], r["occurred_at"]) for r in result] == [
        ("bread", "2026-01-01T00:00:00+00:00"),
        ("rice", "2026-01-01T00:00:00+00:00"),
        ("rice", "2026-01-02T00:00:00+00:00"),
    ]


def test_build_consumptions_normalizes_a_non_utc_offset_to_utc() -> None:
    from datetime import timedelta, timezone

    tracked = {1: "milk"}
    minus_five = timezone(timedelta(hours=-5))
    consumptions = [
        {
            "id": 1,
            "item_id": 1,
            "occurred_at": datetime(2026, 1, 10, 3, 0, tzinfo=minus_five),
            "note": None,
        },
    ]
    result = export.build_consumptions(consumptions, tracked)
    assert result[0]["occurred_at"] == "2026-01-10T08:00:00+00:00"


# --- offline: the alias-vs-item-name pre-check --------------------------------


def test_find_alias_collisions_catches_a_planted_collision() -> None:
    items = [
        {"name": "Cereal", "akas": ["milk"]},
        {"name": "Milk"},
    ]
    collisions = export.find_alias_collisions(items)
    assert collisions == [{"item": "Cereal", "aka": "milk", "collides_with": "Milk"}]


def test_find_alias_collisions_ignores_a_self_alias() -> None:
    items = [{"name": "Milk", "akas": ["milk", "Milk!"]}]
    assert export.find_alias_collisions(items) == []


def test_find_alias_collisions_is_clean_with_no_collision() -> None:
    items = [{"name": "Milk", "akas": ["whole milk"]}, {"name": "Eggs", "akas": ["large eggs"]}]
    assert export.find_alias_collisions(items) == []


def test_find_alias_collisions_ignores_punctuation_and_case() -> None:
    items = [{"name": "Non-Fat Yogurt", "akas": ["yogurt"]}, {"name": "yogurt"}]
    collisions = export.find_alias_collisions(items)
    assert collisions == [{"item": "Non-Fat Yogurt", "aka": "yogurt", "collides_with": "yogurt"}]


# --- offline: normalize --------------------------------------------------


def test_normalize_matches_the_importers_rule() -> None:
    assert export.normalize("Non-Fat Greek Yogurt") == "non fat greek yogurt"
    assert export.normalize("  Milk!! ") == "milk"


# --- offline: SELECT-only proof ----------------------------------------------


_FORBIDDEN_SQL = re.compile(
    r"(?i)\b(insert\s+into|update\s+\w+\s+set|delete\s+from|create\s+table|"
    r"drop\s+table|alter\s+table|truncate\s+table)\b|\.commit\s*\("
)


def test_module_source_has_no_write_or_ddl_statement() -> None:
    source = Path(export.__file__).read_text()
    assert not _FORBIDDEN_SQL.search(source), "export_legacy_pantry.py must stay SELECT-only"


def test_module_source_only_fetches_never_executes() -> None:
    """Every ``asyncpg.Connection`` call in the module is ``.fetch(``, never ``.execute(``."""
    source = Path(export.__file__).read_text()
    assert ".execute(" not in source
    assert source.count("conn.fetch(") >= 5


# --- offline: format_summary and CLI plumbing ---------------------------------


def test_format_summary_lists_every_count() -> None:
    text = export.format_summary(
        {"items": 3, "untracked_skipped": 1, "purchases": 10, "consumptions": 4}, 0
    )
    assert "items: 3 (untracked skipped: 1)" in text
    assert "purchases: 10" in text
    assert "consumptions: 4" in text
    assert "collisions" not in text


def test_format_summary_reports_collision_count() -> None:
    text = export.format_summary(
        {"items": 1, "untracked_skipped": 0, "purchases": 0, "consumptions": 0}, 2
    )
    assert "alias/item-name collisions: 2" in text


def test_main_requires_database_url(monkeypatch, capsys) -> None:
    monkeypatch.delenv(export.DATABASE_URL_ENV, raising=False)
    assert export.main([]) == 1
    assert "DATABASE_URL" in capsys.readouterr().err


async def _fake_run_export_clean(_url: str):
    document = {
        "version": 1,
        "items": [{"name": "Milk"}],
        "purchases": [],
        "consumptions": [],
    }
    summary = {"items": 1, "untracked_skipped": 0, "purchases": 0, "consumptions": 0}
    return document, summary


async def _fake_run_export_colliding(_url: str):
    document = {
        "version": 1,
        "items": [{"name": "Cereal", "akas": ["milk"]}, {"name": "Milk"}],
        "purchases": [],
        "consumptions": [],
    }
    summary = {"items": 2, "untracked_skipped": 0, "purchases": 0, "consumptions": 0}
    return document, summary


def test_main_writes_to_out_file_on_a_clean_export(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(export.DATABASE_URL_ENV, "postgresql://x/y")
    monkeypatch.setattr(export, "run_export", _fake_run_export_clean)
    out = tmp_path / "export.json"

    code = export.main(["--out", str(out)])

    assert code == 0
    written = json.loads(out.read_text())
    assert written["items"] == [{"name": "Milk"}]


def test_main_writes_to_stdout_by_default(monkeypatch, capsys) -> None:
    monkeypatch.setenv(export.DATABASE_URL_ENV, "postgresql://x/y")
    monkeypatch.setattr(export, "run_export", _fake_run_export_clean)

    code = export.main([])

    assert code == 0
    out = capsys.readouterr().out
    assert json.loads(out)["items"] == [{"name": "Milk"}]


def test_main_rejects_a_collision_and_writes_nothing(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(export.DATABASE_URL_ENV, "postgresql://x/y")
    monkeypatch.setattr(export, "run_export", _fake_run_export_colliding)
    out = tmp_path / "export.json"

    code = export.main(["--out", str(out)])

    assert code == 1
    assert not out.exists()


def test_main_force_writes_the_file_despite_a_collision(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv(export.DATABASE_URL_ENV, "postgresql://x/y")
    monkeypatch.setattr(export, "run_export", _fake_run_export_colliding)
    out = tmp_path / "export.json"

    code = export.main(["--out", str(out), "--force"])

    assert code == 0
    assert out.exists()
    assert "collides" in capsys.readouterr().err


def test_main_reports_a_database_error(monkeypatch, capsys) -> None:
    monkeypatch.setenv(export.DATABASE_URL_ENV, "postgresql://x/y")

    async def _boom(_url: str):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(export, "run_export", _boom)
    assert export.main([]) == 1
    assert "connection refused" in capsys.readouterr().err


def test_normalize_dsn_strips_a_driver_suffix() -> None:
    assert export._normalize_dsn("postgresql+asyncpg://u:p@host/db") == "postgresql://u:p@host/db"
    assert export._normalize_dsn("postgresql://u:p@host/db") == "postgresql://u:p@host/db"


# --- integration: throwaway Postgres (old schema) -> exporter -> real import -


_MISSING_LEGACY_DB = f"needs {LEGACY_DB_ENV} pointed at a throwaway Postgres holding the old schema"


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get(LEGACY_DB_ENV), reason=_MISSING_LEGACY_DB)
async def test_export_then_import_round_trip(tmp_path) -> None:
    import asyncpg
    from joshua_pantry import server as pantry_server
    from joshua_pantry.database import build_engine, build_sessionmaker
    from joshua_pantry.migrations import run_migrations

    legacy_url = os.environ[LEGACY_DB_ENV]
    conn = await asyncpg.connect(export._normalize_dsn(legacy_url))
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await conn.execute(SCHEMA_SQL)
        await _seed(conn)

        document, summary = await export.run_export(legacy_url)
    finally:
        await conn.close()

    assert export.find_alias_collisions(document["items"]) == []
    assert summary["untracked_skipped"] == 1
    assert summary["items"] == 4
    assert summary["purchases"] == 18
    assert summary["consumptions"] == 5

    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'pantry.db'}")
    await run_migrations(engine)
    sessionmaker_ = build_sessionmaker(engine)

    previous = pantry_server._sessionmaker
    pantry_server._sessionmaker = sessionmaker_
    try:
        result = await pantry_server.import_data(**document)
        assert result["conflicts"] == []
        assert result["counts"]["items"] == {"created": 4, "merged": 0, "skipped": 0}
        assert result["counts"]["purchases"] == {"created": 18, "merged": 0, "skipped": 0}
        assert result["counts"]["consumptions"] == {"created": 5, "skipped": 0}

        inventory = await pantry_server.get_inventory()
        names = {row["name"] for row in inventory}
        assert names == {"Milk", "Eggs", "Bread", "Rice"}
        assert "Old Discontinued Item" not in names

        milk = next(row for row in inventory if row["name"] == "Milk")
        assert "Whole Milk" in milk["aliases"]

        rice = next(row for row in inventory if row["name"] == "Rice")
        assert rice["avg_cycle_days"] == pytest.approx(14.0)

        bread = next(row for row in inventory if row["name"] == "Bread")
        assert bread["status"] == "out_of_stock"
    finally:
        pantry_server._sessionmaker = previous
        await engine.dispose()


async def _seed(conn) -> None:
    """Synthetic rows only: no real household data ever touches this DB."""
    dairy = await conn.fetchval(
        "INSERT INTO categories (name, normalized) VALUES ($1, $1) RETURNING id", "dairy"
    )
    bakery = await conn.fetchval(
        "INSERT INTO categories (name, normalized) VALUES ($1, $1) RETURNING id", "bakery"
    )
    pantry_cat = await conn.fetchval(
        "INSERT INTO categories (name, normalized) VALUES ($1, $1) RETURNING id", "pantry"
    )

    async def add_item(name: str, category_id, preferred_store, is_tracked: bool) -> int:
        return await conn.fetchval(
            "INSERT INTO items (name, normalized, category_id, preferred_store, is_tracked) "
            "VALUES ($1, $1, $2, $3, $4) RETURNING id",
            name,
            category_id,
            preferred_store,
            is_tracked,
        )

    milk = await add_item("milk", dairy, "aldi", True)
    eggs = await add_item("eggs", dairy, None, True)
    bread = await add_item("bread", bakery, None, True)
    rice = await add_item("rice", pantry_cat, None, True)
    old_item = await add_item("old discontinued item", None, None, False)

    async def add_alias(item_id: int, alias: str) -> None:
        await conn.execute(
            "INSERT INTO item_aliases (item_id, alias, normalized) VALUES ($1, $2, $2)",
            item_id,
            alias,
        )

    await add_alias(milk, "whole milk")
    await add_alias(eggs, "large eggs")

    async def add_purchase(item_id: int, day: date, store, cost) -> None:
        await conn.execute(
            "INSERT INTO purchase_records (item_id, purchased_at, purchase_date, store, unit_cost) "
            "VALUES ($1, $2, $3, $4, $5)",
            item_id,
            datetime(day.year, day.month, day.day, tzinfo=UTC),
            day,
            store,
            cost,
        )

    for i, day in enumerate(
        [date(2026, 1, 5), date(2026, 1, 12), date(2026, 1, 19), date(2026, 1, 26)]
    ):
        await add_purchase(milk, day, "aldi", 3.5 + i * 0.05)

    for i, day in enumerate(
        [date(2026, 1, 6), date(2026, 1, 20), date(2026, 2, 3), date(2026, 2, 17)]
    ):
        await add_purchase(eggs, day, "kroger" if i % 2 else "aldi", 2.5 + i * 0.1)

    bread_days = [date(2026, 1, 7), date(2026, 1, 14), date(2026, 1, 21), date(2026, 1, 28)]
    for i, day in enumerate(bread_days):
        await add_purchase(bread, day, "aldi", 2.75 + i * 0.05)

    rice_days = [
        date(2026, 1, 1),
        date(2026, 1, 15),
        date(2026, 1, 29),
        date(2026, 2, 12),
        date(2026, 2, 26),
        date(2026, 3, 12),
    ]
    for i, day in enumerate(rice_days):
        await add_purchase(rice, day, "aldi", 2.2 + i * 0.02)

    await add_purchase(old_item, date(2026, 1, 3), "aldi", 1.0)
    await add_purchase(old_item, date(2026, 2, 3), "aldi", 1.0)

    async def add_consumption(item_id: int, when: datetime, note: str | None) -> None:
        await conn.execute(
            "INSERT INTO consumption_events (item_id, occurred_at, note) VALUES ($1, $2, $3)",
            item_id,
            when,
            note,
        )

    await add_consumption(milk, datetime(2026, 1, 10, 8, 0, tzinfo=UTC), "cereal")
    await add_consumption(milk, datetime(2026, 1, 24, 8, 0, tzinfo=UTC), "cereal again")
    await add_consumption(eggs, datetime(2026, 1, 8, 8, 0, tzinfo=UTC), "omelette")
    # After Bread's last purchase (2026-01-28): drives it out_of_stock.
    await add_consumption(bread, datetime(2026, 2, 15, 12, 0, tzinfo=UTC), "sandwich")
    await add_consumption(rice, datetime(2026, 1, 5, 18, 0, tzinfo=UTC), "stir fry")
    await add_consumption(old_item, datetime(2026, 1, 4, 18, 0, tzinfo=UTC), "gone")
