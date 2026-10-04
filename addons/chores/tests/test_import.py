"""``importer.import_data``: the batch format, the checks, and a safe second run."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from joshua_chores import importer, service
from joshua_chores.models import Chore, Completion, Member, Transaction
from sqlalchemy import func, select

FIXTURE = Path(__file__).parent / "fixtures" / "import_v1.json"


def _batch() -> dict:
    return json.loads(FIXTURE.read_text())


def _run(session, batch: dict):
    return importer.import_data(
        session,
        batch["version"],
        batch.get("members"),
        batch.get("chores"),
        batch.get("completions"),
        batch.get("transactions"),
    )


async def _row_counts(session) -> dict[str, int]:
    return {
        model.__tablename__: await session.scalar(select(func.count()).select_from(model))
        for model in (Member, Chore, Completion, Transaction)
    }


async def test_import_twice_creates_then_skips(sessionmaker_) -> None:
    batch = _batch()
    expected: dict[str, int] = defaultdict(int)
    for row in batch["transactions"]:
        expected[row["member"]] += row["amount"]
    expected["gamma"] = 0

    async with sessionmaker_() as session:
        first = await _run(session, batch)
    async with sessionmaker_() as session:
        counts_after_first = await _row_counts(session)
        second = await _run(session, batch)
        counts_after_second = await _row_counts(session)

    assert first["counts"] == {
        "members": {"created": 3, "skipped": 0},
        "chores": {"created": 3, "skipped": 0},
        "completions": {"created": 3, "skipped": 0},
        "transactions": {"created": 5, "skipped": 0},
    }
    assert second["counts"] == {
        "members": {"created": 0, "skipped": 3},
        "chores": {"created": 0, "skipped": 3},
        "completions": {"created": 0, "skipped": 3},
        "transactions": {"created": 0, "skipped": 5},
    }
    assert counts_after_first == counts_after_second
    assert first["balances"] == dict(expected)
    assert second["balances"] == dict(expected)


async def test_import_keeps_the_dates_and_timestamps(session) -> None:
    await _run(session, _batch())

    dishes = await session.scalar(select(Chore).where(Chore.import_id == "chore-1"))
    assert dishes.next_due_date == date(2026, 2, 3)
    assert dishes.last_completed_at == datetime(2026, 2, 2, 18, 0, tzinfo=UTC)
    assert dishes.created_at == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)

    bins = await session.scalar(select(Chore).where(Chore.name == "Bins"))
    assert bins.created_at == datetime(2026, 1, 5, 7, 0, tzinfo=UTC)
    garage = await session.scalar(select(Chore).where(Chore.name == "Garage"))
    assert garage.is_active is False

    spent = await session.scalar(select(Transaction).where(Transaction.import_id == "tx-3"))
    assert spent.created_at == datetime(2026, 2, 3, 17, 0, tzinfo=UTC)
    assert spent.actor == "mcp"
    rows = await service.ledger(session, spent.member_id)
    assert [row.import_id for row in rows] == ["tx-3", "tx-2", "tx-1"]

    completion = await session.scalar(
        select(Completion).where(Completion.import_id == "completion-2")
    )
    assert completion.completed_at == datetime(2026, 2, 2, 18, 0, tzinfo=UTC)
    assert completion.note == "fast"
    assert completion.chore_id == dishes.id
    linked = await session.scalar(select(Transaction).where(Transaction.import_id == "tx-2"))
    assert linked.reference_id == completion.id

    by_name = await session.scalar(select(Completion).where(Completion.import_id == "completion-3"))
    assert by_name.chore_id == bins.id

    gamma = await service.get_member_by_slug(session, "gamma")
    assert gamma.is_active is False


async def test_a_later_batch_can_name_existing_rows(session) -> None:
    batch = _batch()
    await importer.import_data(session, 1, batch["members"], batch["chores"])
    result = await importer.import_data(
        session, 1, [], None, batch["completions"], batch["transactions"]
    )
    assert result["counts"]["completions"]["created"] == 3
    assert result["counts"]["transactions"]["created"] == 5
    assert result["balances"] == {"alpha": -15, "beta": 45}

    more = await importer.import_data(
        session,
        1,
        [],
        transactions=[
            {
                "external_id": "tx-6",
                "member": "alpha",
                "amount": 1,
                "description": "Late",
                "source": "adjustment",
                "created_at": "2026-02-04T00:00:00",
                "completion_external_id": "completion-1",
            }
        ],
    )
    assert more["balances"] == {"alpha": -14}
    row = await session.scalar(select(Transaction).where(Transaction.import_id == "tx-6"))
    assert row.created_at == datetime(2026, 2, 4, tzinfo=UTC)
    assert row.reference_id is not None


async def test_a_bad_version_is_refused(session) -> None:
    with pytest.raises(service.InvalidArgument, match="version must be 1"):
        await importer.import_data(session, 2, [])


async def test_a_bad_batch_writes_nothing_and_names_each_path(session) -> None:
    batch = _batch()
    batch["members"][1]["slug"] = "Bad Slug"
    batch["chores"][0]["points"] = 0
    batch["chores"][1]["frequency"] = "hourly"
    batch["completions"][0]["external_chore_id"] = "chore-404"
    batch["completions"][1]["member"] = "alpha"
    batch["completions"][1]["chore"] = "Dishes"
    batch["transactions"][0]["source"] = "gift"
    batch["transactions"][4]["external_id"] = "tx-2"
    batch["transactions"][2]["colour"] = "red"
    batch["transactions"][3]["completion_external_id"] = "completion-404"

    with pytest.raises(service.InvalidArgument) as caught:
        await _run(session, batch)
    message = str(caught.value)
    for path in (
        "members[1].slug",
        "chores[0].points",
        "chores[1]",
        "chores[2].member: member 'beta' does not exist",
        "completions[0].external_chore_id: chore 'chore-404' does not exist",
        "completions[1]: Value error, give external_chore_id, or member and chore",
        "transactions[0]: Value error, source must be one of",
        "transactions[4]: 'tx-2' is in the batch more than one time",
        "transactions[2].colour",
        "transactions[3].completion_external_id: completion 'completion-404'",
    ):
        assert path in message
    assert await _row_counts(session) == {
        "members": 0,
        "chores": 0,
        "completions": 0,
        "transactions": 0,
    }


async def test_a_chore_name_must_name_one_chore(session) -> None:
    members = [{"slug": "alpha", "name": "Alpha"}]
    chores = [
        {
            "member": "alpha",
            "name": "Dishes",
            "points": 1,
            "frequency": "daily",
            "next_due_date": "2026-01-01",
            "created_at": created,
        }
        for created in ("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
    ]
    completion = {
        "external_id": "c-1",
        "member": "alpha",
        "chore": "Dishes",
        "completed_at": "2026-01-03T00:00:00Z",
        "points_awarded": 1,
    }
    with pytest.raises(service.InvalidArgument, match="more than one chore"):
        await importer.import_data(session, 1, members, chores, [completion])
    assert (await _row_counts(session))["members"] == 0

    missing = {**completion, "chore": "Nothing"}
    with pytest.raises(service.InvalidArgument, match="'Nothing' of member 'alpha' does not exist"):
        await importer.import_data(session, 1, members, chores[:1], [missing])
    assert (await _row_counts(session))["chores"] == 0
