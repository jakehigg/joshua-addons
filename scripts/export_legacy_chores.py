#!/usr/bin/env python3
"""Export the old chores app's Postgres data to a chores import file.

The old chores app (a different repository) keeps its data in four tables:
``kids``, ``chores``, ``completions``, and ``transactions``. This script
reads them and writes one JSON file in the ``version: 1`` import format
(``addons/chores/docs/import.md``).

Every query below is a SELECT. The connection never calls commit. This
script writes nothing to the old database.

This script targets the OLD schema, so it does not import
``joshua_chores``. The document builder takes rows that are already
fetched, so a test can call it with no database driver.

Run procedure
-------------

1. Open a port-forward to the Postgres of the old chores app, in one
   terminal::

       kubectl -n <namespace> port-forward svc/<service> 5432:5432

2. Run the export, in a second terminal::

       DATABASE_URL=postgresql://<user>:<password>@localhost:5432/<db> \\
       uv run --with asyncpg python scripts/export_legacy_chores.py --out chores_export.json

   The script prints a summary to stderr: the row count of each table and
   the stored balance of each member.

   The integrity gate can stop the export. See "Integrity gate" below.

3. Import the file into the chores addon::

       uv run python scripts/chores_import.py chores_export.json \\
         --url https://<chores-addon>/mcp --token <ADDON_TOKEN>

   Compare the balances that the import prints with the stored balances
   that the export printed.

What this exports
-----------------

- ``kids`` becomes ``members[]``: slug, name, ``is_active: true``, and a
  ``sort_order`` from the order of the kid ids.
- ``chores`` becomes ``chores[]``. The ``external_id`` is
  ``legacy-chore-<id>``.
- ``completions`` becomes ``completions[]``. The ``external_id`` is
  ``legacy-completion-<id>``. Each row refers to its chore through
  ``external_chore_id``.
- ``transactions`` becomes ``transactions[]``. The ``external_id`` is
  ``legacy-tx-<id>``. The source stays the same. When the source is
  ``chore`` and ``reference_id`` is the id of an exported completion, the
  row gets ``completion_external_id``.

Each timestamp is an ISO value in UTC. Each date is ``YYYY-MM-DD``. A
field with no value stays out of its row.

What this does not export
-------------------------

- ``kids.points_balance``. The new schema has no stored balance. The
  balance is the sum of the ledger. The integrity gate makes sure that
  the two are equal before the export.
- ``chores.updated_at``. The import sets it to ``created_at``.
- ``completions.kid_id``. The new schema takes the member of a completion
  from its chore. The summary counts each completion whose kid is not the
  kid of its chore.
- ``transactions.reference_id`` when the source is not ``chore``, or when
  it does not point at an exported completion.

Integrity gate
--------------

For each kid, ``points_balance`` must be equal to the sum of the
``amount`` values of the transactions of that kid. When a kid fails this
check, the script lists each mismatch on stderr and exits with code 2. It
does not write the file.

``--force`` writes the file anyway and prints a warning. The import then
gives each member the sum of the ledger, not the stored counter.

A row that the import will reject (a slug that is not valid, a chore with
0 points or less, an empty name or description) also stops the export,
with exit code 1. ``--force`` does not change this, because the import
rejects the full batch.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

DATABASE_URL_ENV = "DATABASE_URL"
IMPORT_VERSION = 1
EXIT_ERROR = 1
EXIT_BALANCE_MISMATCH = 2

CHORE_PREFIX = "legacy-chore-"
COMPLETION_PREFIX = "legacy-completion-"
TX_PREFIX = "legacy-tx-"

# A copy of ``joshua_chores.service.SLUG_PATTERN``. This script must not
# import that package. Keep the two the same.
SLUG_RE = re.compile(r"^[a-z0-9-]{1,32}$")

Row = dict[str, Any]


# --- database access (SELECT only) ------------------------------------------


async def fetch_kids(conn: Any) -> list[Row]:
    rows = await conn.fetch("SELECT id, name, slug, points_balance FROM kids ORDER BY id")
    return [dict(row) for row in rows]


async def fetch_chores(conn: Any) -> list[Row]:
    rows = await conn.fetch(
        "SELECT id, kid_id, name, points, frequency, is_active, next_due_date, "
        "last_completed_at, created_at FROM chores ORDER BY id"
    )
    return [dict(row) for row in rows]


async def fetch_completions(conn: Any) -> list[Row]:
    rows = await conn.fetch(
        "SELECT id, chore_id, kid_id, completed_at, points_awarded, note "
        "FROM completions ORDER BY id"
    )
    return [dict(row) for row in rows]


async def fetch_transactions(conn: Any) -> list[Row]:
    rows = await conn.fetch(
        "SELECT id, kid_id, amount, description, source, reference_id, created_at "
        "FROM transactions ORDER BY id"
    )
    return [dict(row) for row in rows]


# --- pure mapping: rows in, the import document out -------------------------


def iso_utc(value: datetime) -> str:
    """Return ``value`` as an ISO string in UTC. A naive value is UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def iso_date(value: date) -> str:
    return value.isoformat()


def build_members(kids: list[Row]) -> list[Row]:
    """Map ``kids`` to ``members[]``. ``sort_order`` follows the kid id order."""
    ordered = sorted(kids, key=lambda row: row["id"])
    return [
        {"slug": kid["slug"], "name": kid["name"], "is_active": True, "sort_order": index}
        for index, kid in enumerate(ordered)
    ]


def build_chores(chores: list[Row], slugs: dict[int, str]) -> list[Row]:
    """Map ``chores`` to ``chores[]``, in the order of the chore ids."""
    result: list[Row] = []
    for chore in sorted(chores, key=lambda row: row["id"]):
        entry: Row = {
            "external_id": f"{CHORE_PREFIX}{chore['id']}",
            "member": slugs[chore["kid_id"]],
            "name": chore["name"],
            "points": chore["points"],
            "frequency": chore["frequency"],
            "is_active": chore["is_active"],
            "next_due_date": iso_date(chore["next_due_date"]),
            "created_at": iso_utc(chore["created_at"]),
        }
        if chore["last_completed_at"] is not None:
            entry["last_completed_at"] = iso_utc(chore["last_completed_at"])
        result.append(entry)
    return result


def build_completions(completions: list[Row]) -> list[Row]:
    """Map ``completions`` to ``completions[]``, in the order of the completion ids.

    Each row refers to its chore through ``external_chore_id``. The format
    does not let a row give a member as well, so the member comes from the
    chore.
    """
    result: list[Row] = []
    for completion in sorted(completions, key=lambda row: row["id"]):
        entry: Row = {
            "external_id": f"{COMPLETION_PREFIX}{completion['id']}",
            "external_chore_id": f"{CHORE_PREFIX}{completion['chore_id']}",
            "completed_at": iso_utc(completion["completed_at"]),
            "points_awarded": completion["points_awarded"],
        }
        if completion["note"]:
            entry["note"] = completion["note"]
        result.append(entry)
    return result


def build_transactions(
    transactions: list[Row], slugs: dict[int, str], completion_ids: set[int]
) -> list[Row]:
    """Map ``transactions`` to ``transactions[]``, in the order of the ledger ids.

    A row with source ``chore`` and a ``reference_id`` in ``completion_ids``
    gets ``completion_external_id``.
    """
    result: list[Row] = []
    for tx in sorted(transactions, key=lambda row: row["id"]):
        entry: Row = {
            "external_id": f"{TX_PREFIX}{tx['id']}",
            "member": slugs[tx["kid_id"]],
            "amount": tx["amount"],
            "description": tx["description"],
            "source": tx["source"],
            "created_at": iso_utc(tx["created_at"]),
        }
        if tx["source"] == "chore" and tx["reference_id"] in completion_ids:
            entry["completion_external_id"] = f"{COMPLETION_PREFIX}{tx['reference_id']}"
        result.append(entry)
    return result


def find_balance_mismatches(kids: list[Row], transactions: list[Row]) -> list[Row]:
    """Return each kid whose ``points_balance`` is not the sum of its ledger."""
    sums: dict[int, int] = {}
    for tx in transactions:
        sums[tx["kid_id"]] = sums.get(tx["kid_id"], 0) + tx["amount"]
    mismatches: list[Row] = []
    for kid in sorted(kids, key=lambda row: row["id"]):
        ledger = sums.get(kid["id"], 0)
        if kid["points_balance"] != ledger:
            mismatches.append(
                {"slug": kid["slug"], "stored": kid["points_balance"], "ledger": ledger}
            )
    return mismatches


def find_format_problems(document: Row) -> list[str]:
    """Return each value that the import will reject.

    This is not the full check of the importer. It catches the values that
    the old schema allows and the new format does not.
    """
    problems: list[str] = []
    for index, member in enumerate(document["members"]):
        if not SLUG_RE.match(member["slug"]):
            problems.append(f"members[{index}].slug: {member['slug']!r} is not a valid slug")
        if not member["name"].strip():
            problems.append(f"members[{index}].name: empty")
    for index, chore in enumerate(document["chores"]):
        if chore["points"] <= 0:
            problems.append(f"chores[{index}].points: {chore['points']} is not more than 0")
        if not chore["name"].strip():
            problems.append(f"chores[{index}].name: empty")
    for index, tx in enumerate(document["transactions"]):
        if not tx["description"].strip():
            problems.append(f"transactions[{index}].description: empty")
    return problems


def build_document(
    kids: list[Row],
    chores: list[Row],
    completions: list[Row],
    transactions: list[Row],
) -> tuple[Row, Row]:
    """Return (the import document, a summary) from rows that are already fetched.

    The rows have the columns of the ``fetch_*`` queries above, with the
    Python types that asyncpg gives: ``datetime`` for a timestamp and
    ``date`` for a date.
    """
    slugs = {kid["id"]: kid["slug"] for kid in kids}
    chore_kids = {chore["id"]: chore["kid_id"] for chore in chores}
    completion_ids = {completion["id"] for completion in completions}

    document: Row = {
        "version": IMPORT_VERSION,
        "members": build_members(kids),
        "chores": build_chores(chores, slugs),
        "completions": build_completions(completions),
        "transactions": build_transactions(transactions, slugs, completion_ids),
    }
    summary: Row = {
        "members": len(document["members"]),
        "chores": len(document["chores"]),
        "completions": len(document["completions"]),
        "transactions": len(document["transactions"]),
        "linked_transactions": sum(
            1 for tx in document["transactions"] if "completion_external_id" in tx
        ),
        "completion_kid_differs": sum(
            1 for row in completions if chore_kids.get(row["chore_id"]) != row["kid_id"]
        ),
        "stored_balances": {
            kid["slug"]: kid["points_balance"] for kid in sorted(kids, key=lambda r: r["id"])
        },
        "mismatches": find_balance_mismatches(kids, transactions),
    }
    return document, summary


# --- orchestration -----------------------------------------------------------


async def export_document(conn: Any) -> tuple[Row, Row]:
    """Read the old database and return (the import document, a summary)."""
    kids = await fetch_kids(conn)
    chores = await fetch_chores(conn)
    completions = await fetch_completions(conn)
    transactions = await fetch_transactions(conn)
    return build_document(kids, chores, completions, transactions)


def _normalize_dsn(url: str) -> str:
    """Remove a ``+driver`` suffix. asyncpg wants a plain ``postgresql://`` DSN."""
    if url.startswith("postgresql+"):
        return "postgresql://" + url.split("://", 1)[1]
    return url


async def run_export(database_url: str) -> tuple[Row, Row]:
    """Connect, export, and close. This is the only place that opens a socket."""
    import asyncpg

    conn = await asyncpg.connect(_normalize_dsn(database_url))
    try:
        return await export_document(conn)
    finally:
        await conn.close()


def format_summary(summary: Row) -> str:
    lines = [
        "export summary:",
        f"  members: {summary['members']}",
        f"  chores: {summary['chores']}",
        f"  completions: {summary['completions']}",
        f"  transactions: {summary['transactions']} "
        f"(linked to a completion: {summary['linked_transactions']})",
    ]
    if summary["completion_kid_differs"]:
        lines.append(
            f"  completions whose kid is not the kid of the chore: "
            f"{summary['completion_kid_differs']}"
        )
    balances = ", ".join(f"{slug}={value}" for slug, value in summary["stored_balances"].items())
    lines.append(f"  stored balances: {balances}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export the old chores app's Postgres data to a chores import file."
    )
    parser.add_argument(
        "--out", type=Path, default=None, help="write the file here; default stdout"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="write the file when a stored balance is not the sum of the ledger",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    database_url = os.environ.get(DATABASE_URL_ENV)
    if not database_url:
        print(
            f"export-legacy-chores: set {DATABASE_URL_ENV} to the Postgres URL of the old app",
            file=sys.stderr,
        )
        return EXIT_ERROR

    try:
        document, summary = asyncio.run(run_export(database_url))
    except Exception as exc:
        print(f"export-legacy-chores: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(format_summary(summary), file=sys.stderr)

    problems = find_format_problems(document)
    if problems:
        print("export-legacy-chores: the import will reject these values:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print("Correct the old data. No file was written.", file=sys.stderr)
        return EXIT_ERROR

    if summary["mismatches"]:
        print(
            "export-legacy-chores: a stored balance is not the sum of the ledger:",
            file=sys.stderr,
        )
        for row in summary["mismatches"]:
            print(
                f"  - {row['slug']}: stored {row['stored']}, ledger sum {row['ledger']}",
                file=sys.stderr,
            )
        if not args.force:
            print("Correct the old data, or use --force. No file was written.", file=sys.stderr)
            return EXIT_BALANCE_MISMATCH
        print(
            "WARNING: --force is set. The import gives each member the ledger sum, "
            "not the stored balance.",
            file=sys.stderr,
        )

    text = json.dumps(document, indent=2) + "\n"
    if args.out:
        args.out.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
