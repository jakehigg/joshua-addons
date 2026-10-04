"""Tests for scripts/export_legacy_chores.py: the export of the old chores app.

All offline. A fake connection gives rows with the Python types that
asyncpg gives, so ``export_document`` runs its real queries and mapping.
``addons/chores/tests/test_import_roundtrip.py`` feeds an export through
the real importer.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts import export_legacy_chores as export

KIDS = [
    {"id": 2, "name": "Beta", "slug": "beta", "points_balance": 5},
    {"id": 1, "name": "Alpha", "slug": "alpha", "points_balance": 25},
]
CHORES = [
    {
        "id": 10,
        "kid_id": 1,
        "name": "Dishes",
        "points": 10,
        "frequency": "daily",
        "is_active": True,
        "next_due_date": date(2026, 3, 2),
        "last_completed_at": datetime(2026, 3, 1, 18, 0, tzinfo=UTC),
        "created_at": datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
    },
    {
        "id": 11,
        "kid_id": 2,
        "name": "Bins",
        "points": 5,
        "frequency": "weekly",
        "is_active": False,
        "next_due_date": date(2026, 3, 9),
        "last_completed_at": None,
        "created_at": datetime(2026, 1, 2, 9, 0, tzinfo=UTC),
    },
]
COMPLETIONS = [
    {
        "id": 100,
        "chore_id": 10,
        "kid_id": 1,
        "completed_at": datetime(2026, 3, 1, 18, 0, tzinfo=UTC),
        "points_awarded": 10,
        "note": "done",
    },
    {
        "id": 101,
        "chore_id": 10,
        "kid_id": 1,
        "completed_at": datetime(2026, 2, 1, 18, 0, tzinfo=UTC),
        "points_awarded": 10,
        "note": None,
    },
]
TRANSACTIONS = [
    {
        "id": 1000,
        "kid_id": 1,
        "amount": 10,
        "description": "Completed: Dishes",
        "source": "chore",
        "reference_id": 100,
        "created_at": datetime(2026, 3, 1, 18, 0, tzinfo=UTC),
    },
    {
        "id": 1001,
        "kid_id": 1,
        "amount": 10,
        "description": "Completed: Dishes",
        "source": "chore",
        "reference_id": 101,
        "created_at": datetime(2026, 2, 1, 18, 0, tzinfo=UTC),
    },
    {
        "id": 1002,
        "kid_id": 1,
        "amount": 15,
        "description": "Bonus",
        "source": "one_off",
        "reference_id": 100,
        "created_at": datetime(2026, 2, 10, 12, 0, tzinfo=UTC),
    },
    {
        "id": 1003,
        "kid_id": 1,
        "amount": -10,
        "description": "Cash out",
        "source": "withdrawal",
        "reference_id": None,
        "created_at": datetime(2026, 2, 20, 12, 0, tzinfo=UTC),
    },
    {
        "id": 1004,
        "kid_id": 2,
        "amount": 5,
        "description": "Completed: Bins",
        "source": "chore",
        "reference_id": 999,
        "created_at": datetime(2026, 1, 9, 7, 0, tzinfo=UTC),
    },
]


class FakeConnection:
    """Answer the four SELECTs of the exporter from in-memory rows."""

    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self.tables = tables
        self.queries: list[str] = []

    async def fetch(self, query: str) -> list[dict[str, Any]]:
        self.queries.append(query)
        table = re.search(r"\bFROM (\w+)", query).group(1)
        return [dict(row) for row in self.tables[table]]


def _tables(**overrides: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    tables = {
        "kids": KIDS,
        "chores": CHORES,
        "completions": COMPLETIONS,
        "transactions": TRANSACTIONS,
    }
    tables.update(overrides)
    return tables


async def _export(**overrides: list[dict[str, Any]]):
    conn = FakeConnection(_tables(**overrides))
    document, summary = await export.export_document(conn)
    return conn, document, summary


# --- the document -------------------------------------------------------------


async def test_export_document_has_the_version_1_shape() -> None:
    conn, document, summary = await _export()

    assert set(document) == {"version", "members", "chores", "completions", "transactions"}
    assert document["version"] == 1
    assert all(query.lstrip().upper().startswith("SELECT") for query in conn.queries)
    assert len(conn.queries) == 4
    assert summary["members"] == 2
    assert summary["chores"] == 2
    assert summary["completions"] == 2
    assert summary["transactions"] == 5


async def test_members_follow_the_kid_id_order() -> None:
    _, document, _ = await _export()
    assert document["members"] == [
        {"slug": "alpha", "name": "Alpha", "is_active": True, "sort_order": 0},
        {"slug": "beta", "name": "Beta", "is_active": True, "sort_order": 1},
    ]


async def test_chores_keep_every_field_and_omit_a_null() -> None:
    _, document, _ = await _export()
    assert document["chores"] == [
        {
            "external_id": "legacy-chore-10",
            "member": "alpha",
            "name": "Dishes",
            "points": 10,
            "frequency": "daily",
            "is_active": True,
            "next_due_date": "2026-03-02",
            "created_at": "2026-01-01T09:00:00+00:00",
            "last_completed_at": "2026-03-01T18:00:00+00:00",
        },
        {
            "external_id": "legacy-chore-11",
            "member": "beta",
            "name": "Bins",
            "points": 5,
            "frequency": "weekly",
            "is_active": False,
            "next_due_date": "2026-03-09",
            "created_at": "2026-01-02T09:00:00+00:00",
        },
    ]


async def test_completions_refer_to_the_chore_and_are_in_id_order() -> None:
    _, document, _ = await _export()
    assert document["completions"] == [
        {
            "external_id": "legacy-completion-100",
            "external_chore_id": "legacy-chore-10",
            "completed_at": "2026-03-01T18:00:00+00:00",
            "points_awarded": 10,
            "note": "done",
        },
        {
            "external_id": "legacy-completion-101",
            "external_chore_id": "legacy-chore-10",
            "completed_at": "2026-02-01T18:00:00+00:00",
            "points_awarded": 10,
        },
    ]
    chore_ids = {chore["external_id"] for chore in document["chores"]}
    assert all(row["external_chore_id"] in chore_ids for row in document["completions"])


async def test_only_a_chore_transaction_with_an_exported_completion_is_linked() -> None:
    _, document, summary = await _export()
    links = {tx["external_id"]: tx.get("completion_external_id") for tx in document["transactions"]}
    assert links == {
        "legacy-tx-1000": "legacy-completion-100",
        "legacy-tx-1001": "legacy-completion-101",
        # A one_off row keeps no link, even with a reference_id.
        "legacy-tx-1002": None,
        "legacy-tx-1003": None,
        # A reference_id that is not an exported completion gives no link.
        "legacy-tx-1004": None,
    }
    assert summary["linked_transactions"] == 2
    completion_ids = {row["external_id"] for row in document["completions"]}
    assert all(
        tx["completion_external_id"] in completion_ids
        for tx in document["transactions"]
        if "completion_external_id" in tx
    )


async def test_transactions_keep_member_amount_description_and_source() -> None:
    _, document, _ = await _export()
    withdrawal = document["transactions"][3]
    assert withdrawal == {
        "external_id": "legacy-tx-1003",
        "member": "alpha",
        "amount": -10,
        "description": "Cash out",
        "source": "withdrawal",
        "created_at": "2026-02-20T12:00:00+00:00",
    }


async def test_timestamps_are_the_same_instant_in_utc() -> None:
    minus_five = timezone(timedelta(hours=-5))
    local = datetime(2026, 2, 1, 13, 0, tzinfo=minus_five)
    completions = [dict(COMPLETIONS[1], completed_at=local)]
    naive = datetime(2026, 1, 1, 9, 0)
    chores = [dict(CHORES[0], created_at=naive), CHORES[1]]

    _, document, _ = await _export(completions=completions, chores=chores)

    exported = document["completions"][0]["completed_at"]
    assert exported == "2026-02-01T18:00:00+00:00"
    assert datetime.fromisoformat(exported) == local
    assert document["chores"][0]["created_at"] == "2026-01-01T09:00:00+00:00"
    for tx, row in zip(document["transactions"], TRANSACTIONS, strict=True):
        assert datetime.fromisoformat(tx["created_at"]) == row["created_at"]


async def test_summary_counts_a_completion_whose_kid_is_not_the_chore_kid() -> None:
    completions = [dict(COMPLETIONS[0], kid_id=2), COMPLETIONS[1]]
    _, _, summary = await _export(completions=completions)
    assert summary["completion_kid_differs"] == 1


# --- the integrity gate -------------------------------------------------------


def test_find_balance_mismatches_is_empty_when_the_ledger_matches() -> None:
    assert export.find_balance_mismatches(KIDS, TRANSACTIONS) == []


def test_find_balance_mismatches_lists_each_bad_kid() -> None:
    kids = [dict(KIDS[0], points_balance=7), dict(KIDS[1], points_balance=0)]
    assert export.find_balance_mismatches(kids, TRANSACTIONS) == [
        {"slug": "alpha", "stored": 0, "ledger": 25},
        {"slug": "beta", "stored": 7, "ledger": 5},
    ]


def test_find_balance_mismatches_counts_a_kid_with_no_ledger_as_zero() -> None:
    kids = [{"id": 3, "name": "Gamma", "slug": "gamma", "points_balance": 4}]
    assert export.find_balance_mismatches(kids, []) == [{"slug": "gamma", "stored": 4, "ledger": 0}]


def _fake_run_export(tables: dict[str, list[dict[str, Any]]]):
    async def _run(_url: str):
        return await export.export_document(FakeConnection(tables))

    return _run


def test_main_writes_the_file_on_a_clean_export(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv(export.DATABASE_URL_ENV, "postgresql://x/y")
    monkeypatch.setattr(export, "run_export", _fake_run_export(_tables()))
    out = tmp_path / "export.json"

    assert export.main(["--out", str(out)]) == 0

    written = json.loads(out.read_text())
    assert [member["slug"] for member in written["members"]] == ["alpha", "beta"]
    err = capsys.readouterr().err
    assert "members: 2" in err
    assert "transactions: 5 (linked to a completion: 2)" in err
    assert "stored balances: alpha=25, beta=5" in err


def test_main_writes_to_stdout_without_out(monkeypatch, capsys) -> None:
    monkeypatch.setenv(export.DATABASE_URL_ENV, "postgresql://x/y")
    monkeypatch.setattr(export, "run_export", _fake_run_export(_tables()))

    assert export.main([]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == 1


def test_main_exits_2_on_a_mismatch_and_writes_nothing(monkeypatch, tmp_path, capsys) -> None:
    kids = [dict(KIDS[0], points_balance=99), KIDS[1]]
    monkeypatch.setenv(export.DATABASE_URL_ENV, "postgresql://x/y")
    monkeypatch.setattr(export, "run_export", _fake_run_export(_tables(kids=kids)))
    out = tmp_path / "export.json"

    assert export.main(["--out", str(out)]) == 2

    assert not out.exists()
    err = capsys.readouterr().err
    assert "beta: stored 99, ledger sum 5" in err


def test_main_force_writes_the_file_with_a_warning(monkeypatch, tmp_path, capsys) -> None:
    kids = [dict(KIDS[0], points_balance=99), KIDS[1]]
    monkeypatch.setenv(export.DATABASE_URL_ENV, "postgresql://x/y")
    monkeypatch.setattr(export, "run_export", _fake_run_export(_tables(kids=kids)))
    out = tmp_path / "export.json"

    assert export.main(["--out", str(out), "--force"]) == 0

    assert out.exists()
    assert "WARNING" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"kids": [dict(KIDS[0], slug="Beta Kid"), KIDS[1]]}, "is not a valid slug"),
        ({"kids": [dict(KIDS[0], name="  "), KIDS[1]]}, "members[1].name: empty"),
        ({"chores": [dict(CHORES[0], points=0), CHORES[1]]}, "is not more than 0"),
        ({"chores": [dict(CHORES[0], name=""), CHORES[1]]}, "chores[0].name: empty"),
        (
            {"transactions": [dict(TRANSACTIONS[0], description=" "), *TRANSACTIONS[1:]]},
            "transactions[0].description: empty",
        ),
    ],
)
def test_main_exits_1_on_a_value_the_import_rejects(
    monkeypatch, tmp_path, capsys, overrides, message
) -> None:
    monkeypatch.setenv(export.DATABASE_URL_ENV, "postgresql://x/y")
    monkeypatch.setattr(export, "run_export", _fake_run_export(_tables(**overrides)))
    out = tmp_path / "export.json"

    assert export.main(["--out", str(out), "--force"]) == 1

    assert not out.exists()
    assert message in capsys.readouterr().err


def test_main_requires_database_url(monkeypatch, capsys) -> None:
    monkeypatch.delenv(export.DATABASE_URL_ENV, raising=False)
    assert export.main([]) == 1
    assert "DATABASE_URL" in capsys.readouterr().err


def test_main_reports_a_database_error(monkeypatch, capsys) -> None:
    monkeypatch.setenv(export.DATABASE_URL_ENV, "postgresql://x/y")

    async def _boom(_url: str):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(export, "run_export", _boom)
    assert export.main([]) == 1
    assert "connection refused" in capsys.readouterr().err


async def test_run_export_connects_reads_and_closes(monkeypatch) -> None:
    import asyncpg

    conn = FakeConnection(_tables())
    closed: list[bool] = []
    seen: list[str] = []

    async def _close() -> None:
        closed.append(True)

    conn.close = _close

    async def _connect(dsn: str):
        seen.append(dsn)
        return conn

    monkeypatch.setattr(asyncpg, "connect", _connect)
    document, _ = await export.run_export("postgresql+asyncpg://u:p@host/db")

    assert seen == ["postgresql://u:p@host/db"]
    assert closed == [True]
    assert document["version"] == 1


def test_normalize_dsn_strips_a_driver_suffix() -> None:
    assert export._normalize_dsn("postgresql+asyncpg://u:p@h/db") == "postgresql://u:p@h/db"
    assert export._normalize_dsn("postgresql://u:p@h/db") == "postgresql://u:p@h/db"


# --- SELECT only, and no dependency on the new package -----------------------


_FORBIDDEN_SQL = re.compile(
    r"(?i)\b(insert\s+into|update\s+\w+\s+set|delete\s+from|create\s+table|"
    r"drop\s+table|alter\s+table|truncate\s+table)\b|\.commit\s*\("
)


def test_module_source_has_no_write_or_ddl_statement() -> None:
    source = Path(export.__file__).read_text()
    assert not _FORBIDDEN_SQL.search(source), "export_legacy_chores.py must stay SELECT-only"


def test_module_source_only_fetches_never_executes() -> None:
    source = Path(export.__file__).read_text()
    assert ".execute(" not in source
    assert source.count("conn.fetch(") == 4


def test_module_never_imports_the_new_package() -> None:
    source = Path(export.__file__).read_text()
    assert not re.search(r"^\s*(from|import)\s+joshua_chores", source, re.MULTILINE)
