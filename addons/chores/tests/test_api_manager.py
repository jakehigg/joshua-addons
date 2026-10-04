"""The manager routes under ``/api``: the PIN guard and each write."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import api_client

PIN = "test-pin-4242"
MANAGER = {"Authorization": f"Bearer {PIN}"}

# Each manager route, with a body that is valid after ``_seed``.
MANAGER_ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("POST", "/api/members", {"slug": "beta", "name": "Beta"}),
    ("PATCH", "/api/members/alpha", {"name": "Alpha Two"}),
    ("POST", "/api/chores", {"slug": "alpha", "name": "Trash", "points": 5, "frequency": "daily"}),
    ("PATCH", "/api/chores/1", {"points": 20}),
    ("PUT", "/api/chores/1", {"points": 20}),
    ("DELETE", "/api/chores/1", None),
    ("POST", "/api/members/alpha/award", {"points": 5, "description": "Bonus"}),
    ("POST", "/api/members/alpha/deduct", {"points": 5, "description": "Spent"}),
    ("GET", "/api/transactions", None),
]
ROUTE_IDS = [f"{method} {path}" for method, path, _ in MANAGER_ROUTES]
SUCCESS = {"POST /api/members": 201, "POST /api/chores": 201, "DELETE /api/chores/1": 204}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch) -> None:
    for name in ("XP_PER_DOLLAR", "COOLDOWN_SECONDS", "MANAGER_LABEL", "CHORES_TZ"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MANAGER_PIN", PIN)


@pytest.fixture
def app(app_factory):
    return app_factory(None)


async def _seed(client) -> int:
    """Add the member ``alpha`` and the chore 1."""
    await client.post("/api/members", json={"slug": "alpha", "name": "Alpha"}, headers=MANAGER)
    response = await client.post(
        "/api/chores",
        json={"slug": "alpha", "name": "Dishes", "points": 10, "frequency": "daily"},
        headers=MANAGER,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _send(client, method: str, path: str, body: Any, headers: dict[str, Any]):
    return await client.request(method, path, json=body, headers=headers)


@pytest.mark.parametrize(("method", "path", "body"), MANAGER_ROUTES, ids=ROUTE_IDS)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-pin"},
        {"Authorization": PIN},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer pïn".encode("latin-1")},
    ],
    ids=["missing", "wrong", "no-scheme", "empty", "non-ascii"],
)
async def test_manager_route_refuses_a_missing_or_wrong_pin(
    app, method: str, path: str, body: Any, headers: dict[str, Any]
) -> None:
    async with api_client(app) as client:
        await _seed(client)
        response = await _send(client, method, path, body, headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}


@pytest.mark.parametrize(("method", "path", "body"), MANAGER_ROUTES, ids=ROUTE_IDS)
async def test_manager_route_accepts_the_right_pin(app, method: str, path: str, body: Any) -> None:
    async with api_client(app) as client:
        await _seed(client)
        response = await _send(client, method, path, body, MANAGER)
    assert response.status_code == SUCCESS.get(f"{method} {path}", 200), response.text


@pytest.mark.parametrize(("method", "path", "body"), MANAGER_ROUTES, ids=ROUTE_IDS)
async def test_manager_route_gives_503_when_no_pin_is_set(
    app_factory, monkeypatch, method: str, path: str, body: Any
) -> None:
    monkeypatch.delenv("MANAGER_PIN")
    app = app_factory(None)
    async with api_client(app) as client:
        for headers in ({}, {"Authorization": "Bearer "}, MANAGER):
            response = await _send(client, method, path, body, headers)
            assert response.status_code == 503
            assert response.json() == {"detail": "MANAGER_PIN is not set"}


async def test_add_member_and_a_duplicate(app) -> None:
    async with api_client(app) as client:
        created = await client.post(
            "/api/members", json={"slug": "alpha", "name": "Alpha"}, headers=MANAGER
        )
        again = await client.post(
            "/api/members", json={"slug": "alpha", "name": "Other"}, headers=MANAGER
        )
        bad_slug = await client.post(
            "/api/members", json={"slug": "Bad Slug", "name": "X"}, headers=MANAGER
        )
        missing = await client.post("/api/members", json={"slug": "gamma"}, headers=MANAGER)
    assert created.status_code == 201
    assert created.json() == {
        "slug": "alpha",
        "name": "Alpha",
        "is_active": True,
        "sort_order": 0,
        "balance": 0,
        "balance_dollars": "$0.00",
    }
    assert again.status_code == 400
    assert "already exists" in again.json()["detail"]
    assert bad_slug.status_code == 400
    assert missing.status_code == 400
    assert missing.json() == {"detail": "name: Field required"}


async def test_change_member(app) -> None:
    async with api_client(app) as client:
        await _seed(client)
        response = await client.patch(
            "/api/members/alpha",
            json={"name": "Alpha Two", "is_active": False, "sort_order": 3},
            headers=MANAGER,
        )
        unknown = await client.patch("/api/members/nobody", json={}, headers=MANAGER)
        wrong_type = await client.patch(
            "/api/members/alpha", json={"is_active": "no"}, headers=MANAGER
        )
    assert response.status_code == 200
    assert response.json()["name"] == "Alpha Two"
    assert response.json()["is_active"] is False
    assert response.json()["sort_order"] == 3
    assert unknown.status_code == 404
    assert wrong_type.status_code == 400
    assert wrong_type.json()["detail"].startswith("is_active:")


async def test_add_chore_with_a_date_and_the_default_date(app) -> None:
    later = (datetime.now(UTC).date() + timedelta(days=4)).isoformat()
    async with api_client(app) as client:
        await _seed(client)
        dated = await client.post(
            "/api/chores",
            json={
                "slug": "alpha",
                "name": "Lawn",
                "points": 30,
                "frequency": "weekly",
                "next_due_date": later,
            },
            headers=MANAGER,
        )
        default = await client.post(
            "/api/chores",
            json={"slug": "alpha", "name": "Bed", "points": 1, "frequency": "daily"},
            headers=MANAGER,
        )
    assert dated.status_code == 201
    assert dated.json()["next_due_date"] == later
    assert dated.json()["slug"] == "alpha"
    assert default.json()["next_due_date"] == datetime.now(UTC).date().isoformat()


@pytest.mark.parametrize(
    ("body", "status", "detail"),
    [
        (
            {"slug": "alpha", "name": "X", "points": 0, "frequency": "daily"},
            400,
            "points must be a whole number more than 0",
        ),
        (
            {"slug": "alpha", "name": "X", "points": "10", "frequency": "daily"},
            400,
            "points: Input should be a valid integer",
        ),
        (
            {"slug": "alpha", "name": "X", "points": 1, "frequency": "hourly"},
            400,
            "frequency must be one of",
        ),
        (
            {
                "slug": "alpha",
                "name": "X",
                "points": 1,
                "frequency": "daily",
                "next_due_date": "soon",
            },
            400,
            "next_due_date:",
        ),
        (
            {"slug": "nobody", "name": "X", "points": 1, "frequency": "daily"},
            404,
            "nobody",
        ),
    ],
    ids=["zero-points", "string-points", "bad-frequency", "bad-date", "unknown-member"],
)
async def test_add_chore_refuses_a_bad_value(app, body, status: int, detail: str) -> None:
    async with api_client(app) as client:
        await _seed(client)
        response = await client.post("/api/chores", json=body, headers=MANAGER)
    assert response.status_code == status
    assert detail in response.json()["detail"]


@pytest.mark.parametrize("method", ["PATCH", "PUT"])
async def test_change_chore(app, method: str) -> None:
    later = (datetime.now(UTC).date() + timedelta(days=2)).isoformat()
    async with api_client(app) as client:
        chore_id = await _seed(client)
        response = await client.request(
            method,
            f"/api/chores/{chore_id}",
            json={"name": "Pots", "points": 15, "frequency": "weekly", "next_due_date": later},
            headers=MANAGER,
        )
        retired = await client.request(
            method, f"/api/chores/{chore_id}", json={"is_active": False}, headers=MANAGER
        )
        unknown = await client.request(method, "/api/chores/999", json={}, headers=MANAGER)
    assert response.status_code == 200
    assert response.json() == {
        "slug": "alpha",
        "id": chore_id,
        "name": "Pots",
        "points": 15,
        "frequency": "weekly",
        "is_active": True,
        "next_due_date": later,
        "last_completed_at": None,
    }
    assert retired.json()["is_active"] is False
    assert unknown.status_code == 404


async def test_change_chore_refuses_an_unknown_field(app) -> None:
    async with api_client(app) as client:
        chore_id = await _seed(client)
        response = await client.patch(
            f"/api/chores/{chore_id}", json={"kid_id": 1}, headers=MANAGER
        )
    assert response.status_code == 400
    assert response.json() == {"detail": "kid_id: Extra inputs are not permitted"}


async def test_delete_chore_retires_it(app, tmp_path) -> None:
    async with api_client(app) as client:
        chore_id = await _seed(client)
        response = await client.delete(f"/api/chores/{chore_id}", headers=MANAGER)
        listed = await client.get("/api/chores")
        unknown = await client.delete("/api/chores/999", headers=MANAGER)
        complete = await client.post(f"/api/chores/{chore_id}/complete")
    assert response.status_code == 204
    assert response.content == b""
    assert listed.json()["chores"] == []
    assert unknown.status_code == 404
    assert complete.status_code == 404
    with closing(sqlite3.connect(tmp_path / "chores.db")) as conn:
        row = conn.execute("SELECT is_active FROM chores WHERE id = ?", (chore_id,)).fetchone()
    assert row == (0,)


async def test_award_and_deduct(app, tmp_path) -> None:
    async with api_client(app) as client:
        await _seed(client)
        awarded = await client.post(
            "/api/members/alpha/award",
            json={"points": 40, "description": "Bonus"},
            headers=MANAGER,
        )
        deducted = await client.post(
            "/api/members/alpha/deduct",
            json={"points": 50, "description": "Toy"},
            headers=MANAGER,
        )
        zero = await client.post(
            "/api/members/alpha/award", json={"points": 0, "description": "x"}, headers=MANAGER
        )
        unknown = await client.post(
            "/api/members/nobody/deduct", json={"points": 1, "description": "x"}, headers=MANAGER
        )
    assert awarded.status_code == 200
    assert awarded.json()["balance"] == 40
    assert awarded.json()["transaction"]["amount"] == 40
    assert awarded.json()["transaction"]["source"] == "one_off"
    assert deducted.json()["balance"] == -10
    assert deducted.json()["balance_dollars"] == "-$0.10"
    assert deducted.json()["transaction"]["amount"] == -50
    assert deducted.json()["transaction"]["source"] == "withdrawal"
    assert zero.status_code == 400
    assert unknown.status_code == 404
    with closing(sqlite3.connect(tmp_path / "chores.db")) as conn:
        actors = conn.execute("SELECT DISTINCT actor FROM transactions").fetchall()
    assert actors == [("api",)]


async def test_transactions_of_all_members_and_of_one(app) -> None:
    async with api_client(app) as client:
        await _seed(client)
        await client.post("/api/members", json={"slug": "beta", "name": "Beta"}, headers=MANAGER)
        for slug in ("alpha", "beta"):
            await client.post(
                f"/api/members/{slug}/award",
                json={"points": 1, "description": slug},
                headers=MANAGER,
            )
        everything = await client.get("/api/transactions", headers=MANAGER)
        beta = await client.get("/api/transactions", params={"slug": "beta"}, headers=MANAGER)
        limited = await client.get("/api/transactions", params={"limit": 1}, headers=MANAGER)
        unknown = await client.get("/api/transactions", params={"slug": "nobody"}, headers=MANAGER)
    assert {row["slug"] for row in everything.json()} == {"alpha", "beta"}
    assert [row["slug"] for row in beta.json()] == ["beta"]
    assert len(limited.json()) == 1
    assert unknown.status_code == 404


async def test_a_bad_body_is_checked_after_the_pin(app) -> None:
    async with api_client(app) as client:
        no_pin = await client.post("/api/members", content=b"[]")
        with_pin = await client.post("/api/members", content=b"[]", headers=MANAGER)
    assert no_pin.status_code == 401
    assert with_pin.status_code == 400
    assert with_pin.json() == {"detail": "the body must be a JSON object"}
