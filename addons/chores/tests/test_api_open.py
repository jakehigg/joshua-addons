"""The open routes under ``/api``: the reads, the settings, the version, and complete."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from conftest import api_client

PIN = "test-pin-4242"
MANAGER = {"Authorization": f"Bearer {PIN}"}
SETTINGS = (
    "XP_PER_DOLLAR",
    "COOLDOWN_SECONDS",
    "MANAGER_LABEL",
    "CHORES_TZ",
    "MANAGER_PIN",
    "JOSHUA_ADDONS_VERSION",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch) -> None:
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MANAGER_PIN", PIN)


@pytest.fixture
def app(app_factory):
    return app_factory(None)


async def _seed(client, frequency: str = "daily", due: str | None = None) -> int:
    """Add the member ``alpha`` and one chore. Return the chore id."""
    response = await client.post(
        "/api/members", json={"slug": "alpha", "name": "Alpha"}, headers=MANAGER
    )
    assert response.status_code == 201, response.text
    body = {"slug": "alpha", "name": "Dishes", "points": 10, "frequency": frequency}
    if due is not None:
        body["next_due_date"] = due
    response = await client.post("/api/chores", json=body, headers=MANAGER)
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_settings_give_the_display_values(app, monkeypatch) -> None:
    monkeypatch.setenv("MANAGER_LABEL", "Coach")
    monkeypatch.setenv("XP_PER_DOLLAR", "50")
    monkeypatch.setenv("COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CHORES_TZ", "Europe/Paris")
    async with api_client(app) as client:
        response = await client.get("/api/settings")
    assert response.status_code == 200
    assert response.json() == {
        "manager_label": "Coach",
        "xp_per_dollar": 50,
        "cooldown_seconds": 30,
        "tz": "Europe/Paris",
    }


async def test_settings_never_contain_the_pin(app) -> None:
    async with api_client(app) as client:
        response = await client.get("/api/settings")
    assert PIN not in response.text


@pytest.mark.parametrize("path", ["/version", "/api/version"])
async def test_version_gives_the_env_value(app, monkeypatch, path: str) -> None:
    async with api_client(app) as client:
        assert (await client.get(path)).json() == {"version": "dev"}
        monkeypatch.setenv("JOSHUA_ADDONS_VERSION", "1.2.3")
        assert (await client.get(path)).json() == {"version": "1.2.3"}


async def test_members_list_gives_balances_and_hides_inactive(app) -> None:
    async with api_client(app) as client:
        await _seed(client)
        await client.post("/api/members", json={"slug": "beta", "name": "Beta"}, headers=MANAGER)
        await client.patch("/api/members/beta", json={"is_active": False}, headers=MANAGER)
        await client.post(
            "/api/members/alpha/award",
            json={"points": 150, "description": "Bonus"},
            headers=MANAGER,
        )
        active = await client.get("/api/members")
        everyone = await client.get("/api/members", params={"include_inactive": "true"})
    assert active.status_code == 200
    assert active.json() == [
        {
            "slug": "alpha",
            "name": "Alpha",
            "is_active": True,
            "sort_order": 0,
            "balance": 150,
            "balance_dollars": "$1.50",
        }
    ]
    assert [member["slug"] for member in everyone.json()] == ["alpha", "beta"]


async def test_members_list_refuses_a_bad_flag(app) -> None:
    async with api_client(app) as client:
        response = await client.get("/api/members", params={"include_inactive": "maybe"})
    assert response.status_code == 400
    assert "include_inactive" in response.json()["detail"]


async def test_one_member_and_an_unknown_member(app) -> None:
    async with api_client(app) as client:
        await _seed(client)
        found = await client.get("/api/members/alpha")
        missing = await client.get("/api/members/nobody")
    assert found.json()["slug"] == "alpha"
    assert found.json()["balance"] == 0
    assert missing.status_code == 404
    assert "nobody" in missing.json()["detail"]


async def test_ledger_is_newest_first_and_limited(app) -> None:
    async with api_client(app) as client:
        await _seed(client)
        for points in (1, 2, 3):
            await client.post(
                "/api/members/alpha/award",
                json={"points": points, "description": f"row {points}"},
                headers=MANAGER,
            )
        rows = await client.get("/api/members/alpha/ledger", params={"limit": 2})
        everything = await client.get("/api/members/alpha/ledger")
        bad = await client.get("/api/members/alpha/ledger", params={"limit": "0"})
        word = await client.get("/api/members/alpha/ledger", params={"limit": "ten"})
    assert rows.status_code == 200
    assert [row["description"] for row in rows.json()] == ["row 3", "row 2"]
    assert set(rows.json()[0]) == {"id", "amount", "description", "source", "created_at"}
    assert len(everything.json()) == 3
    assert bad.status_code == 400
    assert word.status_code == 400


async def test_chores_list_with_and_without_a_slug(app) -> None:
    today = datetime.now(UTC).date()
    later = (today + timedelta(days=3)).isoformat()
    async with api_client(app) as client:
        first = await _seed(client, due=later)
        await client.post("/api/members", json={"slug": "beta", "name": "Beta"}, headers=MANAGER)
        response = await client.post(
            "/api/chores",
            json={"slug": "beta", "name": "Trash", "points": 5, "frequency": "weekly"},
            headers=MANAGER,
        )
        second = response.json()["id"]
        everything = await client.get("/api/chores")
        alpha = await client.get("/api/chores", params={"slug": "alpha"})
        overdue = await client.get("/api/chores", params={"overdue_only": "true"})
        unknown = await client.get("/api/chores", params={"slug": "nobody"})

    body = everything.json()
    assert body["today"] == today.isoformat()
    assert [chore["id"] for chore in body["chores"]] == [second, first]
    trash = body["chores"][0]
    assert trash == {
        "id": second,
        "name": "Trash",
        "points": 5,
        "frequency": "weekly",
        "is_active": True,
        "next_due_date": today.isoformat(),
        "last_completed_at": None,
        "slug": "beta",
        "overdue": True,
    }
    assert body["chores"][1]["overdue"] is False
    assert [chore["id"] for chore in alpha.json()["chores"]] == [first]
    assert [chore["id"] for chore in overdue.json()["chores"]] == [second]
    assert unknown.status_code == 404


async def test_chores_list_does_not_write(app, tmp_path) -> None:
    long_ago = (datetime.now(UTC).date() - timedelta(days=10)).isoformat()
    async with api_client(app) as client:
        chore_id = await _seed(client, due=long_ago)
        first = await client.get("/api/chores")
        second = await client.get("/api/chores")
    assert first.json()["chores"] == second.json()["chores"]
    assert first.json()["chores"][0]["next_due_date"] != long_ago
    with closing(sqlite3.connect(tmp_path / "chores.db")) as conn:
        stored = conn.execute("SELECT next_due_date FROM chores WHERE id = ?", (chore_id,))
        assert stored.fetchone()[0] == long_ago


async def test_complete_is_open_and_gives_the_new_balance(app) -> None:
    async with api_client(app) as client:
        chore_id = await _seed(client)
        response = await client.post(f"/api/chores/{chore_id}/complete")
        ledger = await client.get("/api/members/alpha/ledger")
    assert response.status_code == 200
    body = response.json()
    assert body["chore_id"] == chore_id
    assert body["chore_name"] == "Dishes"
    assert body["points_awarded"] == 10
    assert body["balance"] == 10
    assert body["balance_dollars"] == "$0.10"
    assert body["is_active"] is True
    assert ledger.json()[0]["description"] == "Completed: Dishes"


async def test_complete_keeps_the_note_and_the_kiosk_actor(app, tmp_path) -> None:
    async with api_client(app) as client:
        chore_id = await _seed(client)
        response = await client.post(f"/api/chores/{chore_id}/complete", json={"note": "Done"})
    assert response.status_code == 200
    with closing(sqlite3.connect(tmp_path / "chores.db")) as conn:
        assert conn.execute("SELECT note FROM completions").fetchone()[0] == "Done"
        actors = conn.execute("SELECT actor FROM transactions ORDER BY id").fetchall()
    assert actors == [("kiosk",)]


async def test_complete_works_when_no_pin_is_set(app_factory, monkeypatch) -> None:
    monkeypatch.delenv("MANAGER_PIN")
    app = app_factory(None)
    async with api_client(app) as client:
        from joshua_chores import server, service

        async def seed(session):
            member = await service.add_member(session, "alpha", "Alpha")
            today = datetime.now(UTC).date()
            return (await service.add_chore(session, member.id, "Dishes", 10, "daily", today)).id

        chore_id = await server._in_session(seed)
        response = await client.post(f"/api/chores/{chore_id}/complete")
    assert response.status_code == 200
    assert response.json()["balance"] == 10


async def test_complete_twice_gives_409_with_retry_after(app) -> None:
    async with api_client(app) as client:
        chore_id = await _seed(client)
        assert (await client.post(f"/api/chores/{chore_id}/complete")).status_code == 200
        response = await client.post(f"/api/chores/{chore_id}/complete")
    assert response.status_code == 409
    assert int(response.headers["Retry-After"]) > 0
    assert "recently" in response.json()["detail"]


async def test_complete_an_unknown_chore_gives_404(app) -> None:
    async with api_client(app) as client:
        response = await client.post("/api/chores/999/complete")
    assert response.status_code == 404
    assert response.json() == {"detail": "chore 999 does not exist"}


@pytest.mark.parametrize(
    ("content", "detail"),
    [
        (b"not json", "the body must be JSON"),
        (b"[1, 2]", "the body must be a JSON object"),
        (b'{"note": 5}', "note: Input should be a valid string"),
        (b'{"other": 1}', "other: Extra inputs are not permitted"),
    ],
)
async def test_complete_refuses_a_bad_body(app, content: bytes, detail: str) -> None:
    async with api_client(app) as client:
        chore_id = await _seed(client)
        response = await client.post(
            f"/api/chores/{chore_id}/complete",
            content=content,
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 400
    assert response.json() == {"detail": detail}


async def test_startup_warns_when_no_pin_is_set(app_factory, monkeypatch, caplog) -> None:
    monkeypatch.delenv("MANAGER_PIN")
    app = app_factory(None)
    with caplog.at_level(logging.WARNING):
        async with api_client(app):
            pass
    warnings = [r for r in caplog.records if "MANAGER_PIN is not set" in str(r.msg)]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING


async def test_logs_never_contain_the_pin(app, caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        async with api_client(app) as client:
            await client.get("/api/settings")
            await client.post("/api/members", json={"slug": "a", "name": "A"}, headers=MANAGER)
            await client.post(
                "/api/members", json={"slug": "b", "name": "B"}, headers={"Authorization": "x"}
            )
    assert all(PIN not in str(record.msg) for record in caplog.records)
