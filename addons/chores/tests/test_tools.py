"""The MCP tools, each called through an in-process MCP session."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from conftest import mcp_session
from joshua_chores import server
from mcp import ClientSession

SETTINGS = ("XP_PER_DOLLAR", "COOLDOWN_SECONDS", "MANAGER_LABEL", "CHORES_TZ", "MANAGER_PIN")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch) -> None:
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def app(app_factory):
    return app_factory(None)


async def call(session: ClientSession, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await session.call_tool(tool, arguments)
    assert result.is_error is not True, result.content
    return result.structured_content


async def fail(session: ClientSession, tool: str, **arguments: Any) -> str:
    result = await session.call_tool(tool, arguments)
    assert result.is_error is True
    return result.content[0].text


async def _seed(session: ClientSession, frequency: str = "daily") -> int:
    await call(session, "add_member", slug="alpha", name="Alpha")
    chore = await call(
        session, "add_chore", slug="alpha", name="Dishes", points=10, frequency=frequency
    )
    return chore["id"]


def _table_dump(path) -> list:
    with sqlite3.connect(path) as conn:
        return [
            conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in ("members", "chores", "completions", "transactions")
        ]


async def test_list_members_gives_slugs_and_balances(app) -> None:
    async with mcp_session(app) as session:
        await call(session, "add_member", slug="alpha", name="Alpha")
        await call(session, "add_member", slug="beta", name="Beta")
        await call(session, "set_member", slug="beta", is_active=False)
        await call(session, "award_xp", slug="alpha", points=150, description="Bonus")
        result = await call(session, "list_members")
    assert result == {
        "members": [{"slug": "alpha", "name": "Alpha", "balance": 150, "balance_dollars": "$1.50"}]
    }


async def test_list_members_hides_dollars_at_zero(app, monkeypatch) -> None:
    monkeypatch.setenv("XP_PER_DOLLAR", "0")
    async with mcp_session(app) as session:
        await call(session, "add_member", slug="alpha", name="Alpha")
        result = await call(session, "list_members")
    assert result["members"][0]["balance_dollars"] is None


async def test_list_chores_sorts_by_due_date_and_flags_overdue(app) -> None:
    today = datetime.now(UTC).date()
    async with mcp_session(app) as session:
        await call(session, "add_member", slug="alpha", name="Alpha")
        later = (today + timedelta(days=3)).isoformat()
        await call(
            session, "add_chore", slug="alpha", name="Later", points=5, frequency="weekly",
            next_due_date=later,
        )  # fmt: skip
        await call(session, "add_chore", slug="alpha", name="Now", points=5, frequency="daily")
        result = await call(session, "list_chores", slug="alpha")
        overdue = await call(session, "list_chores", slug="alpha", overdue_only=True)
    assert [c["name"] for c in result["chores"]] == ["Now", "Later"]
    assert [c["overdue"] for c in result["chores"]] == [True, False]
    assert result["chores"][0]["next_due_date"] == today.isoformat()
    assert result["chores"][0]["last_completed_at"] is None
    assert [c["name"] for c in overdue["chores"]] == ["Now"]


async def test_list_chores_does_not_write(app_factory, tmp_path) -> None:
    db = tmp_path / "chores.db"
    app = app_factory(None, db_path=db)
    async with mcp_session(app) as session:
        await call(session, "add_member", slug="alpha", name="Alpha")
        await call(
            session, "add_chore", slug="alpha", name="Old", points=5, frequency="daily",
            next_due_date="2020-01-01",
        )  # fmt: skip
        before = _table_dump(db)
        first = await call(session, "list_chores", slug="alpha")
        second = await call(session, "list_chores", slug="alpha")
        after = _table_dump(db)
    assert first == second
    assert before == after


async def test_unknown_slug_is_a_tool_error(app) -> None:
    async with mcp_session(app) as session:
        for tool, args in (
            ("list_chores", {}),
            ("get_balance", {}),
            ("award_xp", {"points": 1, "description": "x"}),
            ("add_chore", {"name": "x", "points": 1, "frequency": "daily"}),
            ("set_member", {"name": "x"}),
        ):
            message = await fail(session, tool, slug="nobody", **args)
            assert "'nobody' does not exist" in message


async def test_complete_chore_awards_xp_and_moves_the_due_date(app) -> None:
    async with mcp_session(app) as session:
        chore_id = await _seed(session)
        result = await call(session, "complete_chore", chore_id=chore_id, note="done")
        chores = await call(session, "list_chores", slug="alpha")
    assert result["chore_name"] == "Dishes"
    assert result["points_awarded"] == 10
    assert result["balance"] == 10
    assert result["balance_dollars"] == "$0.10"
    assert result["is_active"] is True
    assert date.fromisoformat(result["next_due_date"]) > datetime.now(UTC).date() - timedelta(1)
    assert chores["chores"][0]["last_completed_at"] is not None


async def test_complete_chore_cooldown_gives_the_seconds(app) -> None:
    async with mcp_session(app) as session:
        chore_id = await _seed(session)
        await call(session, "complete_chore", chore_id=chore_id)
        message = await fail(session, "complete_chore", chore_id=chore_id)
    assert "try again in" in message
    seconds = int(message.rsplit("in ", 1)[1].split()[0])
    assert 1 <= seconds <= 60


async def test_complete_chore_unknown_id_is_a_tool_error(app) -> None:
    async with mcp_session(app) as session:
        assert "does not exist" in await fail(session, "complete_chore", chore_id=999)


async def test_complete_chore_uses_the_household_date(app, monkeypatch) -> None:
    # 01:30 UTC on 10 March is 21:30 on 9 March in New York.
    monkeypatch.setenv("CHORES_TZ", "America/New_York")
    monkeypatch.setattr(server, "_utc_now", lambda: datetime(2026, 3, 10, 1, 30, tzinfo=UTC))
    async with mcp_session(app) as session:
        chore_id = await _seed(session)
        chore = await call(session, "list_chores", slug="alpha")
        result = await call(session, "complete_chore", chore_id=chore_id)
    assert chore["today"] == "2026-03-09"
    assert chore["chores"][0]["next_due_date"] == "2026-03-09"
    assert result["next_due_date"] == "2026-03-10"


async def test_complete_one_off_retires_it(app) -> None:
    async with mcp_session(app) as session:
        chore_id = await _seed(session, frequency="one_off")
        result = await call(session, "complete_chore", chore_id=chore_id)
        chores = await call(session, "list_chores", slug="alpha")
    assert result["is_active"] is False
    assert result["next_due_date"] is None
    assert chores["chores"] == []


async def test_get_balance_gives_the_ten_newest_rows(app) -> None:
    async with mcp_session(app) as session:
        await call(session, "add_member", slug="alpha", name="Alpha")
        for index in range(12):
            await call(session, "award_xp", slug="alpha", points=1, description=f"row {index}")
        result = await call(session, "get_balance", slug="alpha")
    assert result["balance"] == 12
    assert result["balance_dollars"] == "$0.12"
    assert len(result["ledger"]) == 10
    row = result["ledger"][0]
    assert set(row) == {"id", "amount", "description", "source", "created_at"}
    assert row["source"] == "one_off"
    datetime.fromisoformat(row["created_at"])


async def test_award_xp_returns_the_transaction(app) -> None:
    async with mcp_session(app) as session:
        await call(session, "add_member", slug="alpha", name="Alpha")
        result = await call(session, "award_xp", slug="alpha", points=25, description="Help")
    assert result["balance"] == 25
    assert result["transaction"]["amount"] == 25
    assert result["transaction"]["description"] == "Help"


@pytest.mark.parametrize("points", [0, -5])
async def test_award_and_deduct_refuse_points_that_are_not_positive(app, points: int) -> None:
    async with mcp_session(app) as session:
        await call(session, "add_member", slug="alpha", name="Alpha")
        assert "points" in await fail(
            session, "award_xp", slug="alpha", points=points, description="x"
        )
        assert "points" in await fail(
            session, "deduct_xp", slug="alpha", points=points, description="x"
        )


async def test_deduct_xp_can_go_below_zero(app) -> None:
    async with mcp_session(app) as session:
        await call(session, "add_member", slug="alpha", name="Alpha")
        await call(session, "award_xp", slug="alpha", points=10, description="In")
        result = await call(session, "deduct_xp", slug="alpha", points=30, description="Out")
    assert result["transaction"]["amount"] == -30
    assert result["transaction"]["source"] == "withdrawal"
    assert result["balance"] == -20
    assert result["balance_dollars"] == "-$0.20"


async def test_tool_writes_record_the_mcp_actor(app_factory, tmp_path) -> None:
    db = tmp_path / "chores.db"
    app = app_factory(None, db_path=db)
    async with mcp_session(app) as session:
        chore_id = await _seed(session)
        await call(session, "award_xp", slug="alpha", points=1, description="x")
        await call(session, "deduct_xp", slug="alpha", points=1, description="y")
        await call(session, "complete_chore", chore_id=chore_id)
    with sqlite3.connect(db) as conn:
        actors = {row[0] for row in conn.execute("SELECT actor FROM transactions")}
    assert actors == {"mcp"}


async def test_add_chore_takes_a_due_date_and_checks_it(app) -> None:
    async with mcp_session(app) as session:
        await call(session, "add_member", slug="alpha", name="Alpha")
        chore = await call(
            session, "add_chore", slug="alpha", name="Bins", points=3, frequency="weekly",
            next_due_date="2030-05-01",
        )  # fmt: skip
        bad_date = await fail(
            session, "add_chore", slug="alpha", name="x", points=3, frequency="weekly",
            next_due_date="tomorrow",
        )  # fmt: skip
        bad_frequency = await fail(
            session, "add_chore", slug="alpha", name="x", points=3, frequency="hourly"
        )
    assert chore["slug"] == "alpha"
    assert chore["next_due_date"] == "2030-05-01"
    assert chore["frequency"] == "weekly"
    assert "ISO date" in bad_date
    assert "frequency" in bad_frequency


async def test_add_chore_default_due_date_is_the_household_today(app, monkeypatch) -> None:
    monkeypatch.setenv("CHORES_TZ", "Pacific/Auckland")
    monkeypatch.setattr(server, "_utc_now", lambda: datetime(2026, 6, 1, 20, 0, tzinfo=UTC))
    async with mcp_session(app) as session:
        await call(session, "add_member", slug="alpha", name="Alpha")
        chore = await call(
            session, "add_chore", slug="alpha", name="x", points=1, frequency="daily"
        )
    assert chore["next_due_date"] == "2026-06-02"


async def test_update_chore_changes_only_the_given_fields(app) -> None:
    async with mcp_session(app) as session:
        chore_id = await _seed(session)
        result = await call(
            session, "update_chore", chore_id=chore_id, points=20, next_due_date="2031-01-02"
        )
        missing = await fail(session, "update_chore", chore_id=999, name="x")
        bad = await fail(session, "update_chore", chore_id=chore_id, points=0)
    assert result["name"] == "Dishes"
    assert result["points"] == 20
    assert result["next_due_date"] == "2031-01-02"
    assert "does not exist" in missing
    assert "points" in bad


async def test_retire_chore_hides_it(app) -> None:
    async with mcp_session(app) as session:
        chore_id = await _seed(session)
        result = await call(session, "retire_chore", chore_id=chore_id)
        chores = await call(session, "list_chores", slug="alpha")
        completed = await fail(session, "complete_chore", chore_id=chore_id)
    assert result["is_active"] is False
    assert chores["chores"] == []
    assert "retired" in completed


async def test_add_member_and_set_member(app) -> None:
    async with mcp_session(app) as session:
        added = await call(session, "add_member", slug="alpha", name="Alpha")
        again = await fail(session, "add_member", slug="alpha", name="Other")
        bad = await fail(session, "add_member", slug="Not A Slug", name="x")
        changed = await call(session, "set_member", slug="alpha", name="A", sort_order=3)
    assert added == {"slug": "alpha", "name": "Alpha", "is_active": True, "sort_order": 0}
    assert "already exists" in again
    assert "slug" in bad
    assert changed == {"slug": "alpha", "name": "A", "is_active": True, "sort_order": 3}


async def test_import_data_through_the_tool(app) -> None:
    async with mcp_session(app) as session:
        result = await call(
            session, "import_data", version=1, members=[{"slug": "alpha", "name": "Alpha"}]
        )
        wrong = await fail(session, "import_data", version=2, members=[])
    assert result["counts"]["members"] == {"created": 1, "skipped": 0}
    assert result["balances"] == {"alpha": 0}
    assert "version must be 1" in wrong


def test_settings_are_not_ready_outside_the_lifespan() -> None:
    with pytest.raises(RuntimeError, match="not ready"):
        server._config()
