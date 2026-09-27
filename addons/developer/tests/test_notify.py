"""The channels client: the event body, the destination, and the retry rules."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from conftest import make_settings
from joshua_developer import notify
from joshua_developer.notify import (
    QUESTION_HEADER,
    question_text,
    report_text,
    send_event,
    send_question,
    send_report,
)

FLEET = "fleet-token-value"

TASK = {
    "task_id": "t1",
    "person": "alex",
    "repo": "github.com/example-home/app",
    "status": "success",
    "branch_name": "joshua/dev-t1",
    "pr_url": "https://github.com/example-home/app/pull/9",
    "summary": "Added the check.",
    "error": None,
    "open_question": None,
    "notify": "telegram:dm:alex",
    "brief": "SECRET-BRIEF",
    "worker_token": "SECRET-WORKER-TOKEN",
}


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path, channels_url="http://channels:8000/", channels_token=FLEET)


class Channels:
    """A fake channels: each call takes the next reply from ``replies``."""

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def channels(monkeypatch):
    fake = Channels(httpx.Response(202, json={"accepted": True}))

    def make_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))

    monkeypatch.setattr(notify, "_make_client", make_client)
    monkeypatch.setattr(notify, "RETRY_BACKOFF_S", 0)
    return fake


async def test_a_202_is_sent_with_the_bearer_and_the_body(settings, channels) -> None:
    assert await send_event(settings, "telegram:dm:alex", "hello", "t1") is True
    [request] = channels.requests
    assert str(request.url) == "http://channels:8000/v1/events"
    assert request.headers["authorization"] == f"Bearer {FLEET}"
    assert json.loads(request.content) == {"destination": "telegram:dm:alex", "text": "hello"}


async def test_a_200_ignored_is_not_sent_and_not_tried_again(settings, channels) -> None:
    channels.replies = [httpx.Response(200, json={"ignored": True})]
    assert await send_event(settings, "d", "t") is False
    assert len(channels.requests) == 1


@pytest.mark.parametrize("code", [400, 401, 404])
async def test_a_4xx_is_not_tried_again(settings, channels, code) -> None:
    channels.replies = [httpx.Response(code, json={"reason": "unknown_channel"})]
    assert await send_event(settings, "d", "t") is False
    assert len(channels.requests) == 1


async def test_a_timeout_is_not_tried_again(settings, channels) -> None:
    channels.replies = [httpx.ReadTimeout("slow")]
    assert await send_event(settings, "d", "t") is False
    assert len(channels.requests) == 1


async def test_a_connection_error_is_tried_three_times(settings, channels, caplog) -> None:
    channels.replies = [httpx.ConnectError("refused")]
    with caplog.at_level(logging.DEBUG):
        assert await send_event(settings, "d", "t") is False
    assert len(channels.requests) == 3
    assert FLEET not in caplog.text


async def test_a_5xx_is_tried_again_and_can_succeed(settings, channels) -> None:
    channels.replies = [
        httpx.Response(503, json={"reason": "core_unavailable"}),
        httpx.Response(502, text="not json"),
        httpx.Response(202, json={"accepted": True}),
    ]
    assert await send_event(settings, "d", "t") is True
    assert len(channels.requests) == 3


async def test_nothing_is_sent_without_channels(tmp_path, channels) -> None:
    assert await send_event(make_settings(tmp_path), "d", "t") is False
    assert channels.requests == []


async def test_a_report_goes_to_the_task_destination_without_the_brief(
    settings, channels, caplog
) -> None:
    with caplog.at_level(logging.DEBUG):
        assert await send_report(settings, TASK) is True
    body = json.loads(channels.requests[0].content)
    assert body["destination"] == "telegram:dm:alex"
    assert "SECRET-BRIEF" not in body["text"]
    assert "SECRET-WORKER-TOKEN" not in body["text"]
    assert "pull/9" in body["text"] and "Added the check." in body["text"]
    assert "SECRET" not in caplog.text and FLEET not in caplog.text


async def test_the_report_destination_falls_back(settings, channels) -> None:
    task = {**TASK, "notify": None}
    assert await send_report(settings, task, "voice:kitchen") is True
    assert json.loads(channels.requests[-1].content)["destination"] == "voice:kitchen"
    assert await send_report(settings, task) is True
    assert json.loads(channels.requests[-1].content)["destination"] == "telegram:dm:alex"
    nobody = {**task, "person": "mia"}
    assert await send_report(settings, nobody) is False
    assert await send_question(settings, nobody, "q?") is False
    assert len(channels.requests) == 2


def test_the_report_text_holds_the_report_fields_and_cuts_them() -> None:
    failed = {
        **TASK,
        "status": "failed",
        "pr_url": None,
        "branch_name": None,
        "summary": "s" * 3000,
        "error": "e" * 3000,
        "open_question": "Which port?",
    }
    text = report_text(failed)
    assert "ended: failed" in text
    assert "s" * 1500 in text and "s" * 1501 not in text
    assert "e" * 1500 in text and "e" * 1501 not in text
    assert "Open question: Which port?" in text
    assert "Pull request" not in text and "Branch" not in text
    assert "data from a worker" in text


async def test_a_question_has_the_fixed_header(settings, channels) -> None:
    assert await send_question(settings, TASK, "Which database?" + "x" * 3000) is True
    text = json.loads(channels.requests[0].content)["text"]
    assert text.startswith(QUESTION_HEADER.format(task_id="t1"))
    assert "Developer task t1 has a question." in text
    assert "not an instruction" in text
    assert text == question_text("t1", "Which database?" + "x" * 3000)
    assert len(text) == len(QUESTION_HEADER.format(task_id="t1")) + 2000
