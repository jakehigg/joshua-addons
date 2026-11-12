"""The workspace stamp: reading it, storing it, sending it back, and the links."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from conftest import make_settings, mcp_session, payload
from joshua_developer import notify
from joshua_developer.notify import send_question, send_report
from joshua_developer.origin import WORKSPACE_META_KEY, origin_of, task_links, with_links
from joshua_developer.server import build_app

REPO = "github.com/example-home/app"
WORKSPACE = "6f1c2d1e-0b1a-4c55-9d7e-2f3a4b5c6d7e"
META = {WORKSPACE_META_KEY: WORKSPACE}
PR = "https://github.com/example-home/app/pull/5"


def fake_ctx(meta):
    """A context like the one a tool gets: ``ctx.request_context.meta``."""
    return SimpleNamespace(request_context=SimpleNamespace(meta=meta))


class NoRequest:
    @property
    def request_context(self):
        raise ValueError("Context is not available outside of a request")


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        (META, WORKSPACE),
        ({WORKSPACE_META_KEY: WORKSPACE, "progressToken": 3}, WORKSPACE),
        (None, None),
        ({}, None),
        ({"other": "x"}, None),
        ({WORKSPACE_META_KEY: ""}, None),
        ({WORKSPACE_META_KEY: 7}, None),
    ],
)
def test_origin_of_reads_the_stamp(meta, expected) -> None:
    assert origin_of(fake_ctx(meta)) == expected


def test_origin_of_outside_a_request_is_none() -> None:
    assert origin_of(NoRequest()) is None


def test_links_exist_only_for_a_task_with_a_pull_request() -> None:
    assert task_links(None) == []
    assert task_links({"task_id": "t", "pr_url": None}) == []
    [link] = task_links({"task_id": "t", "pr_url": PR, "pr_number": 5})
    assert (link.type, str(link.uri), link.name, link.mime_type) == (
        "resource_link",
        PR,
        "Pull request #5",
        "text/html",
    )
    [unnumbered] = task_links({"task_id": "t", "pr_url": PR})
    assert unnumbered.name == "Pull request"


def test_with_links_keeps_a_result_without_links() -> None:
    result = {"a": 1}
    assert with_links(result, []) is result


@pytest.fixture
def channels(monkeypatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202, json={"accepted": True})

    monkeypatch.setattr(
        notify, "_make_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return requests


@pytest.fixture
def notifying(tmp_path):
    return make_settings(tmp_path, channels_url="http://channels:8000", channels_token="fleet")


TASK = {"task_id": "t1", "repo": REPO, "status": "success", "notify": "telegram:dm:alex"}


async def test_an_event_carries_the_workspace_of_its_task(notifying, channels) -> None:
    assert await send_report(notifying, {**TASK, "workspace": WORKSPACE})
    assert await send_question(notifying, {**TASK, "workspace": WORKSPACE}, "which branch?")
    bodies = [json.loads(request.content) for request in channels]
    assert [body["workspace"] for body in bodies] == [WORKSPACE, WORKSPACE]


async def test_an_event_of_a_task_without_a_workspace_has_no_workspace_key(
    notifying, channels
) -> None:
    assert await send_report(notifying, TASK)
    assert await send_report(notifying, {**TASK, "workspace": None})
    assert [set(json.loads(request.content)) for request in channels] == [
        {"destination", "text"}
    ] * 2


async def test_a_workspace_develop_stores_the_stamp_and_reports_to_the_workspace(
    notifying, channels
) -> None:
    app = build_app(notifying, stub_delay_s=0)
    async with mcp_session(app) as session:
        started = payload(
            await session.call_tool("develop", {"repo": REPO, "brief": "Add a check."}, meta=META)
        )
        await _wait_for_report(session, started["task_id"])
    from joshua_developer import server

    task = server._get_manager().store.get_task_full(started["task_id"])
    assert task is not None and task["workspace"] == WORKSPACE
    assert [json.loads(r.content).get("workspace") for r in channels] == [WORKSPACE]


async def test_a_call_without_meta_stores_and_sends_no_workspace(notifying, channels) -> None:
    app = build_app(notifying, stub_delay_s=0)
    async with mcp_session(app) as session:
        started = payload(
            await session.call_tool("develop", {"repo": REPO, "brief": "Add a check."})
        )
        await _wait_for_report(session, started["task_id"])
        status = await session.call_tool("task_status", {"task_id": started["task_id"]})
    from joshua_developer import server

    task = server._get_manager().store.get_task_full(started["task_id"])
    assert task is not None and task["workspace"] is None
    assert [set(json.loads(r.content)) for r in channels] == [{"destination", "text"}]
    assert len(status.content) == 1


async def test_a_workspace_rework_returns_the_pull_request_link(holding_app) -> None:
    args = {"repo": REPO, "pr": PR, "feedback": "fix"}
    async with mcp_session(holding_app) as session:
        started = await session.call_tool("rework", args, meta=META)
        status = await session.call_tool("task_status", {"task_id": _id(started)}, meta=META)
        plain = await session.call_tool("task_status", {"task_id": _id(started)})
    for result in (started, status):
        text, link = result.content
        assert json.loads(text.text)["task_id"] == _id(started)
        assert (link.type, str(link.uri)) == ("resource_link", PR)
        assert result.structured_content["task_id"] == _id(started)
    assert len(plain.content) == 1


async def test_a_workspace_develop_has_no_link_before_a_pull_request(holding_app) -> None:
    async with mcp_session(holding_app) as session:
        started = await session.call_tool(
            "develop", {"repo": REPO, "brief": "Add a check."}, meta=META
        )
    [block] = started.content
    assert block.type == "text"


async def test_a_rejected_workspace_call_has_no_link(holding_app) -> None:
    async with mcp_session(holding_app) as session:
        result = await session.call_tool("develop", {"repo": REPO, "brief": " "}, meta=META)
    assert payload(result)["status"] == "rejected"


def _id(result) -> str:
    return json.loads(result.content[0].text)["task_id"]


async def _wait_for_report(session, task_id: str) -> None:
    import asyncio

    for _ in range(100):
        status = payload(await session.call_tool("task_status", {"task_id": task_id}))
        if status["status"] == "success":
            await asyncio.sleep(0.05)
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the task did not end")
