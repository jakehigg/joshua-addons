#!/usr/bin/env python3
"""Export the legacy pantry's Postgres data to a P2.4 import file.

The old pantry (``pantry/backend/app/models.py``, a different repository)
runs in Jake's cluster. This script reads it and writes one JSON file in
the ``version: 1`` import format (``addons/pantry/docs/import.md``).

Every query below is a SELECT. The connection never calls commit. This
script writes nothing to the old database.

This script targets the OLD schema, so it does not import
``joshua_pantry``. See "Validation" below.

Run procedure
-------------

1. Open a read-only port-forward to the old pantry's Postgres, in one
   terminal::

       kubectl -n <namespace> port-forward svc/<old-pantry-postgres> 5432:5432

2. Run the export, in a second terminal::

       DATABASE_URL=postgresql://<user>:<password>@localhost:5432/<db> \\
       uv run --with asyncpg python scripts/export_legacy_pantry.py --out legacy_export.json

   The script prints a summary to stderr: item, purchase, and consumption
   counts, and the count of items this script skipped as untracked.

   An aka that collides with another item's name stops the export. See
   "What this does not export" below.

3. Import the file into the new pantry::

       python3 scripts/pantry_import.py legacy_export.json \\
         --url https://<new-pantry>/mcp --token <ADDON_TOKEN>

What this exports
------------------

Tracked items (``items.is_tracked = true``) become ``items[]``: name,
akas (from ``item_aliases``), category (from ``categories``), and
preferred_store.

No ``preference`` block. The old data has no upc and no sku. The P2.4
importer needs one of the two to set a preference.

A ``purchase_records`` row of a tracked item becomes one row in
``purchases[]``: item name, purchased_at (the ``purchase_date`` column,
an ISO date), store, and cost (from ``unit_cost``).

The old DB already allows at most one purchase per item per day
(``uq_purchase_records_item_date``). The new importer applies the same
rule, so no old row ever collapses into another on import.

A ``consumption_events`` row of a tracked item becomes one row in
``consumptions[]``: item name, occurred_at (a UTC ISO timestamp), and
note.

A field with no value stays out of its row. This script never writes a
null.

What this does not export
--------------------------

This script skips and counts an untracked item instead of exporting it.
It skips that item's purchases and consumption events too.

This script never reads snapshots, snapshot items, events, the derived
``inventory`` table, or the Reminders id (``items.mag_id``). The new
schema has no place for them. The new importer derives inventory itself
from the purchases and consumptions above.

An aka that names another item, and is not an alias of its own item,
makes the P2.4 import reject the whole batch. An item name is unique in
the new schema, so an alias can never point at one.

This script checks for that case before it writes a file, and lists
every offending row on stderr. Pass ``--force`` to export anyway.

Validation
----------

This script does not import ``joshua_pantry``. It targets the old
schema and must stay decoupled from the new package. See the
repository's CLAUDE.md for why one addon never depends on another the
way this script would.

This script cannot run the P2.4 importer's own validator, so it cannot
promise the P2.4 importer will accept a file it writes. The
aka-collision check above is the one hard-failure case this script can
catch on its own.

``tests/test_export_legacy_pantry.py`` proves the rest. An integration
test feeds a real export through the real ``import_data`` path and
checks the result.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import UTC
from pathlib import Path
from typing import Any

import asyncpg

DATABASE_URL_ENV = "DATABASE_URL"
IMPORT_VERSION = 1

_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")


def normalize(name: str) -> str:
    """Lowercase, replace punctuation with spaces, collapse whitespace.

    A copy of ``joshua_pantry.resolution.normalize`` — this script may not
    import that package. Keep the two identical: the P2.4 importer applies
    this same rule when it decides whether an aka collides with an item
    name, and the pre-check below only matches that decision when the
    algorithm here stays in step with it.
    """
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", name.lower())).strip()


# --- database access (SELECT-only) ------------------------------------------


async def fetch_categories(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await conn.fetch("SELECT id, name FROM categories")
    return [dict(row) for row in rows]


async def fetch_items(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """Every item, tracked and untracked — ``build_items`` below splits them."""
    rows = await conn.fetch("SELECT id, name, category_id, preferred_store, is_tracked FROM items")
    return [dict(row) for row in rows]


async def fetch_aliases(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await conn.fetch("SELECT item_id, alias FROM item_aliases")
    return [dict(row) for row in rows]


async def fetch_purchases(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT id, item_id, purchase_date, store, unit_cost FROM purchase_records"
    )
    return [dict(row) for row in rows]


async def fetch_consumptions(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await conn.fetch("SELECT id, item_id, occurred_at, note FROM consumption_events")
    return [dict(row) for row in rows]


# --- pure mapping: rows in, the import file's JSON shapes out --------------


def build_items(
    items: list[dict[str, Any]],
    categories: list[dict[str, Any]],
    aliases: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Map item rows to ``items[]``. Returns the rows and the untracked count.

    ``items`` rows carry ``id``, ``name``, ``category_id``,
    ``preferred_store``, ``is_tracked``. An untracked row is left out of
    the result and counted instead. Sorted by name, then id, so two runs
    over the same data diff clean.
    """
    category_names: dict[int, str] = {row["id"]: row["name"] for row in categories}
    akas_by_item: dict[int, list[str]] = {}
    for row in aliases:
        akas_by_item.setdefault(row["item_id"], []).append(row["alias"])

    ordered: list[tuple[str, int, dict[str, Any]]] = []
    untracked = 0
    for item in items:
        if not item["is_tracked"]:
            untracked += 1
            continue

        entry: dict[str, Any] = {"name": item["name"]}
        akas = sorted(akas_by_item.get(item["id"], []))
        if akas:
            entry["akas"] = akas
        category_name = category_names.get(item["category_id"])
        if category_name:
            entry["category"] = category_name
        if item["preferred_store"]:
            entry["preferred_store"] = item["preferred_store"]
        ordered.append((item["name"], item["id"], entry))

    ordered.sort(key=lambda row: (row[0], row[1]))
    return [entry for _, _, entry in ordered], untracked


def build_purchases(
    purchases: list[dict[str, Any]], tracked_names: dict[int, str]
) -> list[dict[str, Any]]:
    """Map ``purchase_records`` rows of tracked items to ``purchases[]``.

    A row whose ``item_id`` is not in ``tracked_names`` is left out — an
    untracked item's purchase history is not exported. Sorted by item
    name, then date, then id, so two runs over the same data diff clean.
    """
    ordered: list[tuple[str, str, int, dict[str, Any]]] = []
    for row in purchases:
        name = tracked_names.get(row["item_id"])
        if name is None:
            continue
        purchased_at = row["purchase_date"].isoformat()
        entry: dict[str, Any] = {"item": name, "purchased_at": purchased_at}
        if row["store"]:
            entry["store"] = row["store"]
        if row["unit_cost"] is not None:
            entry["cost"] = row["unit_cost"]
        ordered.append((name, purchased_at, row["id"], entry))

    ordered.sort(key=lambda row: (row[0], row[1], row[2]))
    return [entry for *_key, entry in ordered]


def build_consumptions(
    consumptions: list[dict[str, Any]], tracked_names: dict[int, str]
) -> list[dict[str, Any]]:
    """Map ``consumption_events`` rows of tracked items to ``consumptions[]``.

    A row whose ``item_id`` is not in ``tracked_names`` is left out. Every
    timestamp is rendered UTC ISO, the same way every run over the same
    data renders it, so re-exporting never changes the text of a row that
    did not change. Sorted by item name, then timestamp, then id.
    """
    ordered: list[tuple[str, str, int, dict[str, Any]]] = []
    for row in consumptions:
        name = tracked_names.get(row["item_id"])
        if name is None:
            continue
        occurred_at = row["occurred_at"].astimezone(UTC).isoformat()
        entry: dict[str, Any] = {"item": name, "occurred_at": occurred_at}
        if row["note"]:
            entry["note"] = row["note"]
        ordered.append((name, occurred_at, row["id"], entry))

    ordered.sort(key=lambda row: (row[0], row[1], row[2]))
    return [entry for *_key, entry in ordered]


def find_alias_collisions(items_json: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Find every aka that names a different item's own name.

    The P2.4 importer rejects a batch where an aka matches another item's
    name (an item name is unique in the new schema; an alias can never
    point at one). The old DB's ``item_aliases.normalized`` column is
    globally unique, so two items in this export can never claim the same
    aka — the only collision left to catch is an aka against a name.
    """
    name_by_norm: dict[str, str] = {normalize(item["name"]): item["name"] for item in items_json}
    collisions: list[dict[str, str]] = []
    for item in items_json:
        own_norm = normalize(item["name"])
        for aka in item.get("akas", []):
            aka_norm = normalize(aka)
            if not aka_norm or aka_norm == own_norm:
                continue
            other = name_by_norm.get(aka_norm)
            if other is not None:
                collisions.append({"item": item["name"], "aka": aka, "collides_with": other})
    return collisions


# --- orchestration -----------------------------------------------------------


async def export_document(conn: asyncpg.Connection) -> tuple[dict[str, Any], dict[str, int]]:
    """Read the old database and return (the import document, a count summary)."""
    categories = await fetch_categories(conn)
    items = await fetch_items(conn)
    aliases = await fetch_aliases(conn)
    purchases = await fetch_purchases(conn)
    consumptions = await fetch_consumptions(conn)

    items_json, untracked = build_items(items, categories, aliases)
    tracked_names = {item["id"]: item["name"] for item in items if item["is_tracked"]}
    purchases_json = build_purchases(purchases, tracked_names)
    consumptions_json = build_consumptions(consumptions, tracked_names)

    document = {
        "version": IMPORT_VERSION,
        "items": items_json,
        "purchases": purchases_json,
        "consumptions": consumptions_json,
    }
    summary = {
        "items": len(items_json),
        "untracked_skipped": untracked,
        "purchases": len(purchases_json),
        "consumptions": len(consumptions_json),
    }
    return document, summary


def _normalize_dsn(url: str) -> str:
    """asyncpg wants a bare ``postgresql://`` DSN.

    A person may copy ``DATABASE_URL`` from the new pantry's own env,
    which carries a ``+asyncpg`` (or another) driver suffix SQLAlchemy
    needs and asyncpg's own ``connect`` does not accept.
    """
    if url.startswith("postgresql+"):
        return "postgresql://" + url.split("://", 1)[1]
    return url


async def run_export(database_url: str) -> tuple[dict[str, Any], dict[str, int]]:
    """Connect read-only, export, and close. The one place this script opens a socket."""
    conn = await asyncpg.connect(_normalize_dsn(database_url))
    try:
        return await export_document(conn)
    finally:
        await conn.close()


def format_summary(summary: dict[str, int], collision_count: int) -> str:
    lines = [
        "export summary:",
        f"  items: {summary['items']} (untracked skipped: {summary['untracked_skipped']})",
        f"  purchases: {summary['purchases']}",
        f"  consumptions: {summary['consumptions']}",
    ]
    if collision_count:
        lines.append(f"  alias/item-name collisions: {collision_count}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export the legacy pantry's Postgres data to a P2.4 import file."
    )
    parser.add_argument(
        "--out", type=Path, default=None, help="write the file here; default stdout"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="write the file even when an aka collides with another item's name",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    database_url = os.environ.get(DATABASE_URL_ENV)
    if not database_url:
        print(
            f"export-legacy-pantry: set {DATABASE_URL_ENV} to the old pantry's Postgres URL",
            file=sys.stderr,
        )
        return 1

    try:
        document, summary = asyncio.run(run_export(database_url))
    except Exception as exc:
        print(f"export-legacy-pantry: {exc}", file=sys.stderr)
        return 1

    collisions = find_alias_collisions(document["items"])
    if collisions:
        print("export-legacy-pantry: an aka collides with another item's name:", file=sys.stderr)
        for collision in collisions:
            print(
                f"  - {collision['item']!r} has aka {collision['aka']!r}, "
                f"which is already the name of {collision['collides_with']!r}",
                file=sys.stderr,
            )
        if not args.force:
            print("Fix the old data, or pass --force to export anyway.", file=sys.stderr)
            return 1
        print("--force set: exporting anyway.", file=sys.stderr)

    text = json.dumps(document, indent=2) + "\n"
    if args.out:
        args.out.write_text(text)
    else:
        sys.stdout.write(text)

    print(format_summary(summary, len(collisions)), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
