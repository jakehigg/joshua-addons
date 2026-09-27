"""Tests for the worker API client, against the fake manager."""

from __future__ import annotations

import time

import httpx
import pytest
from conftest import MANAGER_URL, TASK_ID, TASK_TOKEN, FakeManager
from joshua_developer_worker.manager_client import ManagerClient, ManagerError, Report, TaskEnded


async def test_brief_sends_the_task_token_and_the_task_id(manager: FakeManager) -> None:
    manager.brief = {"task_id": TASK_ID, "branch": "b"}
    client = manager.client()
    assert (await client.brief())["branch"] == "b"
    await client.aclose()


async def test_a_wrong_token_gets_a_manager_error(manager: FakeManager) -> None:
    client = manager.client(token="wrong")
    with pytest.raises(ManagerError) as info:
        await client.brief()
    assert info.value.status == 401
    assert info.value.reason == "unauthorized"
    assert "wrong" not in str(info.value)
    await client.aclose()


async def test_a_brief_503_names_the_reason(manager: FakeManager) -> None:
    manager.brief_status = 503
    client = manager.client()
    with pytest.raises(ManagerError) as info:
        await client.brief()
    assert (info.value.status, info.value.reason) == (503, "token_missing")
    await client.aclose()


async def test_a_brief_that_is_not_an_object_is_refused(manager: FakeManager) -> None:
    manager.brief = []  # type: ignore[assignment]
    client = manager.client()
    with pytest.raises(ManagerError):
        await client.brief()
    await client.aclose()


async def test_running_log_and_report(manager: FakeManager) -> None:
    client = manager.client()
    await client.running()
    assert manager.running
    assert await client.log("line one\n") is True
    assert manager.logs == ["line one\n"]
    await client.report(Report(status="success", summary="done"))
    assert manager.reports[0]["status"] == "success"
    await client.aclose()


async def test_ask_waits_for_the_answer(manager: FakeManager) -> None:
    manager.answer_after_polls = 3
    client = manager.client()
    answer = await client.ask("Which port?", time.monotonic() + 10)
    assert answer == "Use port 8080."
    assert manager.questions == ["Which port?"]
    assert manager.polls == 3
    await client.aclose()


async def test_ask_returns_none_when_the_deadline_passes(manager: FakeManager) -> None:
    manager.answer_after_polls = 10_000
    client = manager.client()
    assert await client.ask("Which port?", time.monotonic() + 0.05, poll_pause_s=0.01) is None
    await client.aclose()


async def test_ask_raises_task_ended_on_409(manager: FakeManager) -> None:
    manager.answer_after_polls = 10_000
    client = manager.client()
    await client.ask("Which port?", time.monotonic() - 1)
    manager.ended = True
    with pytest.raises(TaskEnded):
        await client.ask("Again?", time.monotonic() + 10)
    await client.aclose()


async def test_409_is_never_retried() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(409, json={"error": "task_ended"})

    client = ManagerClient(
        MANAGER_URL, TASK_ID, TASK_TOKEN, transport=httpx.MockTransport(handler), backoff_s=0
    )
    with pytest.raises(TaskEnded):
        await client.running()
    assert calls == ["/worker/running"]
    await client.aclose()


async def test_connection_errors_get_three_more_attempts() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 4:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"status": "running"})

    client = ManagerClient(
        MANAGER_URL, TASK_ID, TASK_TOKEN, transport=httpx.MockTransport(handler), backoff_s=0
    )
    await client.running()
    assert len(calls) == 4
    await client.aclose()


async def test_connection_errors_stop_after_the_retries() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("refused", request=request)

    client = ManagerClient(
        MANAGER_URL, TASK_ID, TASK_TOKEN, transport=httpx.MockTransport(handler), backoff_s=0
    )
    with pytest.raises(httpx.ConnectError):
        await client.running()
    assert len(calls) == 4
    await client.aclose()


async def test_a_reply_that_is_not_json_has_an_unknown_reason() -> None:
    client = ManagerClient(
        MANAGER_URL,
        TASK_ID,
        TASK_TOKEN,
        transport=httpx.MockTransport(lambda r: httpx.Response(500, text="no")),
        backoff_s=0,
    )
    with pytest.raises(ManagerError) as info:
        await client.running()
    assert info.value.reason == "unknown"
    await client.aclose()


def test_the_report_refuses_an_unknown_field() -> None:
    with pytest.raises(ValueError):
        Report(status="success", note="x")  # type: ignore[call-arg]
