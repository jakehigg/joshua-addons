"""``GET /v1/jobs``: the jobs route that a viewer reads."""

from __future__ import annotations

from typing import Any

import httpx
from conftest import ADDON, ALEX, MIA
from joshua_developer import server
from joshua_developer.store import ALLOWED_UPDATE_FIELDS


def store() -> Any:
    return server._manager.store  # the store of the app the fixture built


def add_task(
    task_id: str,
    *,
    person: str = "alex",
    status: str = "running",
    **extra: Any,
) -> None:
    """Put one task in the store, and set the fields a worker would write."""
    fields: dict[str, Any] = {
        "task_id": task_id,
        "person": person,
        "task_type": "develop",
        "repo": "github.com/example-home/app",
        "scope": f"branch:{task_id}",
        "persona": "opus",
        "brief": "the brief",
        "branch_name": f"joshua/{task_id}",
    }
    updates = {key: extra.pop(key) for key in list(extra) if key in ALLOWED_UPDATE_FIELDS}
    fields.update(extra)
    store().create_task(**fields)
    store().update_task(task_id, status=status, **updates)


async def get_jobs(app, query: str = "", token: str | None = ADDON) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(f"/v1/jobs{query}", headers=headers)


async def jobs_of(app, query: str = "", token: str | None = ADDON) -> list[dict[str, Any]]:
    response = await get_jobs(app, query, token)
    assert response.status_code == 200
    return response.json()["jobs"]


async def test_one_job_has_the_fields_the_viewer_reads(app) -> None:
    add_task(
        "task-1",
        workspace="ws-7",
        started_at="2026-01-02T03:04:05+00:00",
        input_tokens=1000,
        output_tokens=234,
        estimated_cost=0.5,
    )
    [job] = await jobs_of(app)
    assert job == {
        "id": "task-1",
        "kind": "develop",
        "project": "github.com/example-home/app",
        "branch": "joshua/task-1",
        "status": "running",
        "started_at": "2026-01-02T03:04:05+00:00",
        "notify_channel": "workspace:ws-7",
        "workspace": "ws-7",
        "question": None,
        "question_at": None,
        "tokens": 1234,
        "cost": 0.5,
    }


async def test_a_job_carries_no_text_of_a_worker(app) -> None:
    add_task(
        "task-1",
        summary="the summary",
        error="the error",
        report={"log": "the log"},
        diff_summary="1 file changed",
    )
    [job] = await jobs_of(app)
    for name in ("brief", "summary", "error", "report", "log", "session_log", "feedback"):
        assert name not in job


async def test_the_default_answer_holds_the_active_jobs_only(app) -> None:
    add_task("task-1", status="dispatched")
    add_task("task-2", status="running")
    add_task("task-3", status="success")
    add_task("task-4", status="blocked")
    jobs = await jobs_of(app)
    assert {job["id"] for job in jobs} == {"task-1", "task-2"}


async def test_the_status_query_filters_by_status(app) -> None:
    add_task("task-1", status="running")
    add_task("task-2", status="success")
    add_task("task-3", status="failed")
    jobs = await jobs_of(app, "?status=success,failed")
    assert {job["id"] for job in jobs} == {"task-2", "task-3"}
    assert [job["id"] for job in await jobs_of(app, "?status=blocked")] == []


async def test_a_status_the_addon_does_not_have_matches_no_job(app) -> None:
    add_task("task-1", status="running")
    assert await jobs_of(app, "?status=waiting") == []
    assert await jobs_of(app, "?status=,") == []
    # An empty status query filters nothing: the active jobs answer.
    assert [job["id"] for job in await jobs_of(app, "?status=")] == ["task-1"]


async def test_the_limit_query_caps_the_count(app) -> None:
    for number in range(5):
        add_task(f"task-{number}")
    assert len(await jobs_of(app, "?limit=2")) == 2
    assert len(await jobs_of(app, "?limit=0")) == 1
    assert len(await jobs_of(app, "?limit=nope")) == 5


async def test_the_route_caps_the_limit_itself(app) -> None:
    add_task("task-1")
    assert len(await jobs_of(app, "?limit=100000")) == 1
    assert server.job_limit("100000") == server.MAX_JOBS
    assert server.job_limit(None) == server.MAX_JOBS


async def test_a_job_with_an_open_question_says_so(app) -> None:
    add_task("task-1", open_question="which branch?", asked_at="2026-01-02T03:04:05+00:00")
    [job] = await jobs_of(app)
    assert job["question"] == "which branch?"
    assert job["question_at"] == "2026-01-02T03:04:05+00:00"


async def test_a_job_with_no_question_has_none(app) -> None:
    add_task("task-1")
    [job] = await jobs_of(app)
    assert job["question"] is None
    assert job["question_at"] is None
    assert job["tokens"] == 0
    assert job["cost"] == 0.0


async def test_a_job_of_another_channel_names_that_channel(app) -> None:
    add_task("task-1", notify="telegram:dm:alex")
    [job] = await jobs_of(app)
    assert job["notify_channel"] == "telegram:dm:alex"
    assert job["workspace"] is None


async def test_a_person_reads_their_own_jobs_only(app) -> None:
    add_task("task-1", person="alex")
    add_task("task-2", person="mia")
    assert [job["id"] for job in await jobs_of(app, token=MIA)] == ["task-2"]
    assert [job["id"] for job in await jobs_of(app, token=ALEX)] == ["task-1"]
    assert {job["id"] for job in await jobs_of(app, token=ADDON)} == {"task-1", "task-2"}


async def test_a_missing_bearer_gets_401(app) -> None:
    add_task("task-1")
    assert (await get_jobs(app, token=None)).status_code == 401


async def test_a_wrong_bearer_gets_401(app) -> None:
    add_task("task-1")
    assert (await get_jobs(app, token="nope")).status_code == 401
