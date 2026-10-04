"""Tests for the session, with a fake SDK client. No CLI starts."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock
from conftest import (
    GIT_TOKEN,
    MANAGER_URL,
    TASK_TOKEN,
    FakeManager,
    FakeSDKClient,
    fake_factory,
    sleep_forever,
)
from joshua_developer_worker import git, session
from joshua_developer_worker.clock import Clock

STRUCTURED = {
    "summary": "Added hello.txt.",
    "files_changed": ["hello.txt"],
    "tests_run": "pytest: 3 passed",
    "blocked": None,
}


def result_message(**overrides: Any) -> ResultMessage:
    fields: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 10,
        "duration_api_ms": 8,
        "is_error": False,
        "num_turns": 3,
        "session_id": "s1",
        "total_cost_usd": 0.25,
        "usage": {"input_tokens": 100, "output_tokens": 40},
        "result": "done",
        "structured_output": STRUCTURED,
    }
    fields.update(overrides)
    return ResultMessage(**fields)


def tool_use(name: str, data: dict[str, Any]) -> AssistantMessage:
    return AssistantMessage(content=[ToolUseBlock(id="t1", name=name, input=data)], model="m")


def options_for(tmp_path: Path, manager: FakeManager, clock: Clock) -> Any:
    asker = session.Asker(manager.client(), clock)
    return session.build_options(
        "system",
        tmp_path,
        "claude-opus-5",
        80,
        session.ask_server(asker),
        effort="high",
        env=session.cli_env(MANAGER_URL, TASK_TOKEN, tmp_path / "home", tmp_path / "cfg"),
        stderr=lambda line: None,
    )


def test_build_options_holds_the_tools_and_no_settings(
    tmp_path: Path, manager: FakeManager
) -> None:
    options = options_for(tmp_path, manager, Clock(60))
    assert options.tools == ["Read", "Edit", "Write", "Bash", "Glob", "Grep"]
    assert options.allowed_tools == [*session.BASE_TOOLS, "mcp__manager__ask"]
    assert set(options.disallowed_tools) == {"WebSearch", "WebFetch"}
    assert options.setting_sources == []
    assert options.permission_mode == "bypassPermissions"
    assert options.strict_mcp_config is True
    assert list(options.mcp_servers) == ["manager"]
    assert options.mcp_servers["manager"]["type"] == "sdk"
    assert options.output_format["schema"] == session.RESULT_SCHEMA
    assert options.model == "claude-opus-5"
    assert options.effort == "high"
    assert options.max_turns == 80


def test_the_cli_environment_points_at_the_forwarder(tmp_path: Path) -> None:
    env = session.cli_env(MANAGER_URL + "/", TASK_TOKEN, tmp_path / "h", tmp_path / "c")
    assert env["ANTHROPIC_BASE_URL"] == f"{MANAGER_URL}/worker/claude"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TASK_TOKEN
    assert env["HOME"] == str(tmp_path / "h")
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "c")
    for name in (
        "DISABLE_TELEMETRY",
        "DISABLE_ERROR_REPORTING",
        "DISABLE_AUTOUPDATER",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    ):
        assert env[name] == "1"
    assert not any("PROXY" in key.upper() for key in env)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"tools": ["Read", "WebSearch"]}, "may not hold"),
        ({"allowed_tools": ["WebFetch"]}, "may not hold"),
        ({"tools": ["Read", "Task"]}, "only the file tools"),
        ({"setting_sources": ["project"]}, "setting_sources"),
        ({"setting_sources": None}, "setting_sources"),
    ],
)
def test_check_options_refuses_the_web_and_settings(
    tmp_path: Path, manager: FakeManager, change: dict[str, Any], message: str
) -> None:
    options = options_for(tmp_path, manager, Clock(60))
    for key, value in change.items():
        setattr(options, key, value)
    with pytest.raises(AssertionError, match=message):
        session.check_options(options)


async def test_a_session_records_tools_and_the_structured_result(
    tmp_path: Path, manager: FakeManager
) -> None:
    options = options_for(tmp_path, manager, Clock(60))
    made: list[FakeSDKClient] = []
    script = [
        tool_use("Bash", {"command": "pytest -q " + "x" * 400}),
        AssistantMessage(content=[TextBlock(text="All good.")], model="m"),
        result_message(),
    ]
    log = session.SessionLog(manager.client())
    result = await session.run(
        options, "go", Clock(30), log=log, client_factory=fake_factory(script, made)
    )
    assert result.structured == STRUCTURED
    assert result.is_error is False and result.timed_out is False
    assert result.cost_usd == 0.25
    assert result.usage == {"input_tokens": 100, "output_tokens": 40}
    assert result.text == "done"
    assert result.tool_calls[0]["name"] == "Bash"
    assert len(result.tool_calls[0]["input"]) == session.MAX_TOOL_INPUT
    assert made[0].prompt == "go" and made[0].connected and made[0].disconnected
    assert "tool Bash" in "".join(manager.logs)


async def test_the_ask_tool_round_trip(tmp_path: Path, manager: FakeManager) -> None:
    manager.answer_after_polls = 2
    asker = session.Asker(manager.client(), Clock(30))
    reply = await asker({"question": "Which port?"})
    assert reply == {"content": [{"type": "text", "text": "Use port 8080."}]}
    assert asker.open_question is None
    assert manager.questions == ["Which port?"]


async def test_the_ask_tool_without_an_answer(manager: FakeManager) -> None:
    manager.answer_after_polls = 10_000
    asker = session.Asker(manager.client(), Clock(60), ask_wait_s=0.05)
    reply = await asker({"question": "Which port?"})
    assert reply["content"][0]["text"] == session.no_answer(0)
    assert reply["content"][0]["text"].startswith("No answer arrived in 0 seconds.")
    assert asker.open_question == "Which port?"
    assert manager.stops == 1
    assert asker.clock.paused is False


async def test_the_ask_tool_returns_at_once_when_nobody_can_be_reached(
    manager: FakeManager,
) -> None:
    manager.ask_reply = {"asked": True, "sent": False, "reason": "no_destination"}
    asker = session.Asker(manager.client(), Clock(3600))
    started = time.monotonic()
    reply = await asker({"question": "Which port?"})
    assert time.monotonic() - started < 5
    assert reply == {"content": [{"type": "text", "text": session.NOBODY}]}
    assert "Nobody can be reached" in session.NOBODY and "`blocked`" in session.NOBODY
    assert manager.polls == 0
    # The question stays open, so a stop reports it.
    assert asker.open_question == "Which port?"


async def test_the_ask_tool_refuses_an_empty_question(manager: FakeManager) -> None:
    reply = await session.Asker(manager.client(), Clock(0))({"question": "  "})
    assert reply["is_error"] is True
    assert manager.questions == []


async def test_the_ask_tool_after_the_task_ended(manager: FakeManager) -> None:
    manager.ended = True
    reply = await session.Asker(manager.client(), Clock(5))({"question": "Q?"})
    assert "ended" in reply["content"][0]["text"]


async def test_the_ask_tool_when_the_manager_fails(manager: FakeManager) -> None:
    reply = await session.Asker(manager.client(token="wrong"), Clock(5))({"question": "Q?"})
    assert "could not be sent" in reply["content"][0]["text"]


async def test_the_deadline_stops_the_session(tmp_path: Path, manager: FakeManager) -> None:
    options = options_for(tmp_path, manager, Clock(60))
    made: list[FakeSDKClient] = []
    script = [tool_use("Read", {"file_path": "a.py"}), sleep_forever]
    log = session.SessionLog(manager.client())
    result = await session.run(
        options, "go", Clock(0.2), log=log, client_factory=fake_factory(script, made)
    )
    assert result.timed_out is True
    assert made[0].interrupted and made[0].disconnected
    assert "deadline passed" in log.text()


async def test_a_deadline_in_the_past_starts_nothing(tmp_path: Path, manager: FakeManager) -> None:
    options = options_for(tmp_path, manager, Clock(60))
    made: list[FakeSDKClient] = []
    log = session.SessionLog(None)
    result = await session.run(
        options, "go", Clock(-1), log=log, client_factory=fake_factory([], made)
    )
    assert result.timed_out is True
    assert made[0].connected is False


async def test_an_sdk_error_is_a_failed_session(tmp_path: Path, manager: FakeManager) -> None:
    git.register_secret(GIT_TOKEN)
    options = options_for(tmp_path, manager, Clock(60))
    script = [RuntimeError(f"the CLI died with {GIT_TOKEN}")]
    log = session.SessionLog(manager.client())
    result = await session.run(
        options, "go", Clock(30), log=log, client_factory=fake_factory(script)
    )
    assert result.is_error is True
    assert result.error and "RuntimeError" in result.error
    assert GIT_TOKEN not in result.error
    assert GIT_TOKEN not in log.text()


async def test_an_error_result_is_a_failed_session(tmp_path: Path, manager: FakeManager) -> None:
    options = options_for(tmp_path, manager, Clock(60))
    script = [
        AssistantMessage(content=[TextBlock(text="partial")], model="m"),
        result_message(
            subtype="error_max_turns", is_error=True, result=None, structured_output=None
        ),
    ]
    result = await session.run(
        options,
        "go",
        Clock(30),
        log=session.SessionLog(None),
        client_factory=fake_factory(script),
    )
    assert result.is_error is True
    assert result.error == "the session ended with an error (error_max_turns)"
    assert result.structured is None
    assert result.text == "partial"


async def test_the_log_is_sent_every_interval(tmp_path: Path, manager: FakeManager) -> None:
    options = options_for(tmp_path, manager, Clock(60))

    async def pause(_: FakeSDKClient) -> None:
        await asyncio.sleep(0.15)

    script = [tool_use("Read", {"file_path": "a"}), pause, tool_use("Grep", {"pattern": "b"})]
    log = session.SessionLog(manager.client())
    await session.run(
        options,
        "go",
        Clock(30),
        log=log,
        client_factory=fake_factory(script),
        flush_s=0.05,
    )
    assert len(manager.logs) >= 2
    joined = "".join(manager.logs)
    assert joined.count("tool Read") == 1 and joined.count("tool Grep") == 1


async def test_the_log_stops_after_the_task_ended(manager: FakeManager) -> None:
    log = session.SessionLog(manager.client())
    manager.ended = True
    log.add("one")
    await log.flush()
    manager.ended = False
    log.add("two")
    await log.flush()
    assert manager.logs == []


async def test_a_log_that_fails_is_sent_later(manager: FakeManager) -> None:
    log = session.SessionLog(manager.client(token="wrong"))
    log.add("one")
    await log.flush()
    log.client = manager.client()
    await log.flush()
    assert manager.logs == ["one\n"]


def test_parse_structured() -> None:
    assert session.parse_structured('{"summary": "s", "files_changed": "x", "blocked": " "}') == {
        "summary": "s",
        "files_changed": [],
        "tests_run": "",
        "blocked": None,
    }
    assert session.parse_structured({"summary": "s", "blocked": "Which port?"})["blocked"] == (
        "Which port?"
    )
    assert session.parse_structured("plain text") is None
    assert session.parse_structured("{not json") is None
    assert session.parse_structured({"files_changed": []}) is None
    assert session.parse_structured(None) is None


async def test_the_ask_tool_pauses_the_clock_while_it_waits(manager: FakeManager) -> None:
    now = [0.0]
    clock = Clock(10, now=lambda: now[0])
    manager.answer_after_polls = 3
    manager.poll_delay_s = 0.05
    asker = session.Asker(manager.client(), clock)
    waiting = asyncio.create_task(asker({"question": "Which port?"}))
    await asyncio.sleep(0.02)
    assert clock.paused
    # An hour passes while the question waits. It does not count as work.
    now[0] += 3600
    reply = await waiting
    assert reply["content"][0]["text"] == "Use port 8080."
    assert not clock.paused
    assert clock.remaining() == 10
    assert asker.waited_s > 0
    assert manager.stops == 0


async def test_the_ask_tool_stops_at_the_wait_limit(manager: FakeManager) -> None:
    manager.answer_after_polls = 10_000
    manager.poll_delay_s = 0.02
    clock = Clock(60)
    asker = session.Asker(manager.client(), clock, ask_wait_s=0.1)
    started = time.monotonic()
    reply = await asker({"question": "Which port?"})
    assert time.monotonic() - started < 2
    assert reply["content"][0]["text"].startswith("No answer arrived in 0 seconds.")
    assert manager.stops == 1
    assert not clock.paused
    assert asker.open_question == "Which port?"


async def test_the_wait_limit_is_a_total_for_the_task(manager: FakeManager) -> None:
    manager.answer_after_polls = 10_000
    manager.poll_delay_s = 0.02
    asker = session.Asker(manager.client(), Clock(60), ask_wait_s=1.0)
    # The first question waits about 0.3 s of the 1 s, then gets its answer.
    first = asyncio.create_task(asker({"question": "Which port?"}))
    await asyncio.sleep(0.3)
    manager.answer_after_polls = 0
    reply = await first
    assert reply["content"][0]["text"] == "Use port 8080."
    used = asker.waited_s
    assert 0.25 < used < 0.9
    # The second question gets only what is left, not a full second.
    manager.answer_after_polls = 10_000
    started = time.monotonic()
    reply = await asker({"question": "Which host?"})
    second = time.monotonic() - started
    assert reply["content"][0]["text"].startswith("No answer arrived")
    assert second < 1.0 - used + 0.2
    assert manager.stops == 1
    assert asker.waited_s < 1.0 + 0.2
    # Nothing is left: a third question returns at once, unsent, and stops the wait.
    started = time.monotonic()
    reply = await asker({"question": "Which user?"})
    assert time.monotonic() - started < 0.05
    assert reply["content"][0]["text"] == session.no_wait_left(1.0)
    assert manager.questions == ["Which port?", "Which host?"]
    assert manager.stops == 2
    assert asker.open_question == "Which user?"
    assert not asker.clock.paused


async def test_a_failed_stop_still_returns_the_no_answer_text(manager: FakeManager) -> None:
    manager.answer_after_polls = 10_000

    async def broken() -> int:
        raise httpx.ConnectError("the manager is gone")

    client = manager.client()
    client.stop_ask = broken  # type: ignore[method-assign]
    asker = session.Asker(client, Clock(60), ask_wait_s=0.05)
    reply = await asker({"question": "Which port?"})
    assert reply["content"][0]["text"].startswith("No answer arrived")


def test_duration() -> None:
    assert session.duration(1) == "1 second"
    assert session.duration(45) == "45 seconds"
    assert session.duration(60) == "1 minute"
    assert session.duration(7200) == "120 minutes"


async def test_the_watchdog_stops_the_session_at_the_budget(
    tmp_path: Path, manager: FakeManager
) -> None:
    options = options_for(tmp_path, manager, Clock(60))
    made: list[FakeSDKClient] = []
    started = time.monotonic()
    result = await session.run(
        options,
        "go",
        Clock(0.1),
        log=session.SessionLog(None),
        client_factory=fake_factory([sleep_forever], made),
        watch_s=0.01,
    )
    assert result.timed_out is True
    assert made[0].interrupted
    assert time.monotonic() - started < 2


async def test_the_watchdog_waits_while_the_clock_is_paused(
    tmp_path: Path, manager: FakeManager
) -> None:
    clock = Clock(0.1)
    options = options_for(tmp_path, manager, clock)
    made: list[FakeSDKClient] = []

    async def wait_for_an_answer(_: FakeSDKClient) -> None:
        clock.pause()
        # The wall time passes the budget three times over.
        await asyncio.sleep(0.3)
        clock.resume()

    script = [wait_for_an_answer, result_message()]
    result = await session.run(
        options,
        "go",
        clock,
        log=session.SessionLog(None),
        client_factory=fake_factory(script, made),
        watch_s=0.01,
    )
    assert result.timed_out is False
    assert result.structured == STRUCTURED
    assert not made[0].interrupted
