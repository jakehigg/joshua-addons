"""Round trip: a database in the old chores schema, the exporter, then ``import_data``.

A SQLite file gets the four tables of the old app (``kids``, ``chores``,
``completions``, ``transactions``) and a few months of rows. The exporter
in ``scripts/export_legacy_chores.py`` builds the import document from
those rows. The real importer then loads the document into a new addon
database. The test checks each balance, each chore date, and each ledger
timestamp, and it checks that a second import changes nothing.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from joshua_chores import importer, service
from joshua_chores.models import Chore, Completion, Member, Transaction
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import export_legacy_chores as export  # noqa: E402

# The CREATE statements of the old app's first migration, in SQLite syntax.
OLD_SCHEMA = """
CREATE TABLE kids (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    points_balance INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE chores (
    id INTEGER PRIMARY KEY,
    kid_id INTEGER NOT NULL REFERENCES kids(id),
    name TEXT NOT NULL,
    points INTEGER NOT NULL,
    frequency TEXT NOT NULL
        CHECK (frequency IN ('daily', 'weekly', 'monthly', 'one_off')),
    is_active BOOLEAN NOT NULL DEFAULT 1,
    next_due_date DATE NOT NULL,
    last_completed_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);
CREATE TABLE completions (
    id INTEGER PRIMARY KEY,
    chore_id INTEGER NOT NULL REFERENCES chores(id) ON DELETE CASCADE,
    kid_id INTEGER NOT NULL REFERENCES kids(id),
    completed_at TIMESTAMP NOT NULL,
    points_awarded INTEGER NOT NULL,
    note TEXT
);
CREATE TABLE transactions (
    id INTEGER PRIMARY KEY,
    kid_id INTEGER NOT NULL REFERENCES kids(id),
    amount INTEGER NOT NULL,
    description TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('chore', 'one_off', 'withdrawal')),
    reference_id INTEGER,
    created_at TIMESTAMP NOT NULL
);
"""

KIDS = [(1, "Alpha", "alpha"), (2, "Beta", "beta")]

# id, kid_id, name, points, frequency, is_active, next_due_date,
# last_completed_at, created_at
CHORES = [
    (1, 1, "Dishes", 10, "daily", 1, "2026-04-03", "2026-04-02T18:30:00+00:00",
     "2026-01-01T09:00:00+00:00"),
    (2, 2, "Bins", 5, "weekly", 1, "2026-04-08", "2026-04-01T07:15:00+00:00",
     "2026-01-02T09:00:00+00:00"),
    (3, 1, "Wash the car", 50, "one_off", 0, "2026-02-14", "2026-02-14T15:00:00+00:00",
     "2026-02-01T09:00:00+00:00"),
]  # fmt: skip

# id, chore_id, kid_id, completed_at, points_awarded, note
COMPLETIONS = [
    (1, 1, 1, "2026-01-15T18:00:00+00:00", 10, None),
    (2, 2, 2, "2026-01-21T07:00:00+00:00", 5, "early"),
    (3, 3, 1, "2026-02-14T15:00:00+00:00", 50, None),
    (4, 1, 1, "2026-03-10T18:45:00+00:00", 10, None),
    (5, 2, 2, "2026-04-01T07:15:00+00:00", 5, None),
    (6, 1, 1, "2026-04-02T18:30:00+00:00", 10, "late"),
]

# id, kid_id, amount, description, source, reference_id, created_at
TRANSACTIONS = [
    (1, 1, 10, "Completed: Dishes", "chore", 1, "2026-01-15T18:00:00+00:00"),
    (2, 2, 5, "Completed: Bins", "chore", 2, "2026-01-21T07:00:00+00:00"),
    (3, 1, 50, "Completed: Wash the car", "chore", 3, "2026-02-14T15:00:00+00:00"),
    (4, 2, 20, "Birthday bonus", "one_off", None, "2026-02-20T12:00:00+00:00"),
    (5, 1, -40, "Cash out", "withdrawal", None, "2026-03-01T10:00:00+00:00"),
    (6, 1, 10, "Completed: Dishes", "chore", 4, "2026-03-10T18:45:00+00:00"),
    (7, 2, 5, "Completed: Bins", "chore", 5, "2026-04-01T07:15:00+00:00"),
    (8, 2, -15, "Cash out", "withdrawal", None, "2026-04-01T08:00:00+00:00"),
    (9, 1, 10, "Completed: Dishes", "chore", 6, "2026-04-02T18:30:00+00:00"),
]

# The stored counter of the old app: the sum of the ledger of each kid.
STORED_BALANCES = {"alpha": 40, "beta": 15}


def _old_database(path: Path) -> None:
    db = sqlite3.connect(path)
    try:
        db.executescript(OLD_SCHEMA)
        for kid_id, name, slug in KIDS:
            db.execute(
                "INSERT INTO kids VALUES (?, ?, ?, ?)", (kid_id, name, slug, STORED_BALANCES[slug])
            )
        for row in CHORES:
            db.execute("INSERT INTO chores VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (*row, row[-1]))
        db.executemany("INSERT INTO completions VALUES (?, ?, ?, ?, ?, ?)", COMPLETIONS)
        db.executemany("INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?)", TRANSACTIONS)
        db.commit()
    finally:
        db.close()


def _timestamp(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _fetch(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Read the old rows with the columns and the Python types that asyncpg gives."""
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        kids = [dict(row) for row in db.execute("SELECT id, name, slug, points_balance FROM kids")]
        chores = []
        for row in db.execute(
            "SELECT id, kid_id, name, points, frequency, is_active, next_due_date, "
            "last_completed_at, created_at FROM chores"
        ):
            chore = dict(row)
            chore["is_active"] = bool(chore["is_active"])
            chore["next_due_date"] = date.fromisoformat(chore["next_due_date"])
            chore["last_completed_at"] = _timestamp(chore["last_completed_at"])
            chore["created_at"] = _timestamp(chore["created_at"])
            chores.append(chore)
        completions = []
        for row in db.execute(
            "SELECT id, chore_id, kid_id, completed_at, points_awarded, note FROM completions"
        ):
            completion = dict(row)
            completion["completed_at"] = _timestamp(completion["completed_at"])
            completions.append(completion)
        transactions = []
        for row in db.execute(
            "SELECT id, kid_id, amount, description, source, reference_id, created_at "
            "FROM transactions"
        ):
            tx = dict(row)
            tx["created_at"] = _timestamp(tx["created_at"])
            transactions.append(tx)
    finally:
        db.close()
    return {
        "kids": kids,
        "chores": chores,
        "completions": completions,
        "transactions": transactions,
    }


def _run(session, document: dict):
    return importer.import_data(
        session,
        document["version"],
        document["members"],
        document["chores"],
        document["completions"],
        document["transactions"],
    )


async def _row_counts(session) -> dict[str, int]:
    return {
        model.__tablename__: await session.scalar(select(func.count()).select_from(model))
        for model in (Member, Chore, Completion, Transaction)
    }


async def test_old_database_round_trips_through_the_importer(tmp_path, sessionmaker_) -> None:
    old_path = tmp_path / "old_chores.db"
    _old_database(old_path)
    rows = _fetch(old_path)

    document, summary = export.build_document(
        rows["kids"], rows["chores"], rows["completions"], rows["transactions"]
    )
    assert summary["mismatches"] == []
    assert export.find_format_problems(document) == []
    assert summary["linked_transactions"] == 6

    async with sessionmaker_() as session:
        first = await _run(session, document)
    assert first["counts"] == {
        "members": {"created": 2, "skipped": 0},
        "chores": {"created": 3, "skipped": 0},
        "completions": {"created": 6, "skipped": 0},
        "transactions": {"created": 9, "skipped": 0},
    }
    assert first["balances"] == STORED_BALANCES

    async with sessionmaker_() as session:
        for slug, stored in STORED_BALANCES.items():
            member = await service.get_member_by_slug(session, slug)
            assert await service.balance(session, member.id) == stored

        for old in rows["chores"]:
            chore = await session.scalar(
                select(Chore).where(Chore.import_id == f"legacy-chore-{old['id']}")
            )
            assert chore.next_due_date == old["next_due_date"]
            assert chore.last_completed_at == old["last_completed_at"]
            assert chore.created_at == old["created_at"]
            assert chore.is_active is old["is_active"]
            assert chore.frequency == old["frequency"]

        for old in rows["completions"]:
            completion = await session.scalar(
                select(Completion).where(Completion.import_id == f"legacy-completion-{old['id']}")
            )
            assert completion.completed_at == old["completed_at"]
            assert completion.points_awarded == old["points_awarded"]
            assert completion.note == old["note"]

        for old in rows["transactions"]:
            tx = await session.scalar(
                select(Transaction).where(Transaction.import_id == f"legacy-tx-{old['id']}")
            )
            assert tx.created_at == old["created_at"]
            assert tx.amount == old["amount"]
            assert tx.source == old["source"]
            if old["reference_id"] is None:
                assert tx.reference_id is None
            else:
                linked = await session.get(Completion, tx.reference_id)
                assert linked.import_id == f"legacy-completion-{old['reference_id']}"

        counts_after_first = await _row_counts(session)
        second = await _run(session, document)
        counts_after_second = await _row_counts(session)

    assert second["counts"] == {
        "members": {"created": 0, "skipped": 2},
        "chores": {"created": 0, "skipped": 3},
        "completions": {"created": 0, "skipped": 6},
        "transactions": {"created": 0, "skipped": 9},
    }
    assert second["balances"] == STORED_BALANCES
    assert counts_after_first == counts_after_second


async def test_the_retired_one_off_stays_retired(tmp_path, session) -> None:
    old_path = tmp_path / "old_chores.db"
    _old_database(old_path)
    rows = _fetch(old_path)
    document, _ = export.build_document(
        rows["kids"], rows["chores"], rows["completions"], rows["transactions"]
    )

    await _run(session, document)

    car = await session.scalar(select(Chore).where(Chore.import_id == "legacy-chore-3"))
    assert car.frequency == "one_off"
    assert car.is_active is False
    assert car.last_completed_at == datetime(2026, 2, 14, 15, 0, tzinfo=UTC)
