"""Tests for the session, with a fake SDK client. No CLI starts."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

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


def options_for(tmp_path: Path, manager: FakeManager, deadline: float) -> Any:
    asker = session.Asker(manager.client(), deadline)
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
    options = options_for(tmp_path, manager, time.monotonic() + 60)
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
    options = options_for(tmp_path, manager, time.monotonic() + 60)
    for key, value in change.items():
        setattr(options, key, value)
    with pytest.raises(AssertionError, match=message):
        session.check_options(options)


async def test_a_session_records_tools_and_the_structured_result(
    tmp_path: Path, manager: FakeManager
) -> None:
    options = options_for(tmp_path, manager, time.monotonic() + 60)
    made: list[FakeSDKClient] = []
    script = [
        tool_use("Bash", {"command": "pytest -q " + "x" * 400}),
        AssistantMessage(content=[TextBlock(text="All good.")], model="m"),
        result_message(),
    ]
    log = session.SessionLog(manager.client())
    result = await session.run(
        options, "go", time.monotonic() + 30, log=log, client_factory=fake_factory(script, made)
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
    asker = session.Asker(manager.client(), time.monotonic() + 30)
    reply = await asker({"question": "Which port?"})
    assert reply == {"content": [{"type": "text", "text": "Use port 8080."}]}
    assert asker.open_question is None
    assert manager.questions == ["Which port?"]


async def test_the_ask_tool_without_an_answer(manager: FakeManager) -> None:
    manager.answer_after_polls = 10_000
    asker = session.Asker(manager.client(), time.monotonic() - 1)
    reply = await asker({"question": "Which port?"})
    assert reply["content"][0]["text"] == session.NO_ANSWER
    assert asker.open_question == "Which port?"


async def test_the_ask_tool_refuses_an_empty_question(manager: FakeManager) -> None:
    reply = await session.Asker(manager.client(), 0)({"question": "  "})
    assert reply["is_error"] is True
    assert manager.questions == []


async def test_the_ask_tool_after_the_task_ended(manager: FakeManager) -> None:
    manager.ended = True
    reply = await session.Asker(manager.client(), time.monotonic() + 5)({"question": "Q?"})
    assert "ended" in reply["content"][0]["text"]


async def test_the_ask_tool_when_the_manager_fails(manager: FakeManager) -> None:
    reply = await session.Asker(manager.client(token="wrong"), time.monotonic() + 5)(
        {"question": "Q?"}
    )
    assert "could not be sent" in reply["content"][0]["text"]


async def test_the_deadline_stops_the_session(tmp_path: Path, manager: FakeManager) -> None:
    options = options_for(tmp_path, manager, time.monotonic() + 60)
    made: list[FakeSDKClient] = []
    script = [tool_use("Read", {"file_path": "a.py"}), sleep_forever]
    log = session.SessionLog(manager.client())
    result = await session.run(
        options, "go", time.monotonic() + 0.2, log=log, client_factory=fake_factory(script, made)
    )
    assert result.timed_out is True
    assert made[0].interrupted and made[0].disconnected
    assert "deadline passed" in log.text()


async def test_a_deadline_in_the_past_starts_nothing(tmp_path: Path, manager: FakeManager) -> None:
    options = options_for(tmp_path, manager, time.monotonic() + 60)
    made: list[FakeSDKClient] = []
    log = session.SessionLog(None)
    result = await session.run(
        options, "go", time.monotonic() - 1, log=log, client_factory=fake_factory([], made)
    )
    assert result.timed_out is True
    assert made[0].connected is False


async def test_an_sdk_error_is_a_failed_session(tmp_path: Path, manager: FakeManager) -> None:
    git.register_secret(GIT_TOKEN)
    options = options_for(tmp_path, manager, time.monotonic() + 60)
    script = [RuntimeError(f"the CLI died with {GIT_TOKEN}")]
    log = session.SessionLog(manager.client())
    result = await session.run(
        options, "go", time.monotonic() + 30, log=log, client_factory=fake_factory(script)
    )
    assert result.is_error is True
    assert result.error and "RuntimeError" in result.error
    assert GIT_TOKEN not in result.error
    assert GIT_TOKEN not in log.text()


async def test_an_error_result_is_a_failed_session(tmp_path: Path, manager: FakeManager) -> None:
    options = options_for(tmp_path, manager, time.monotonic() + 60)
    script = [
        AssistantMessage(content=[TextBlock(text="partial")], model="m"),
        result_message(
            subtype="error_max_turns", is_error=True, result=None, structured_output=None
        ),
    ]
    result = await session.run(
        options,
        "go",
        time.monotonic() + 30,
        log=session.SessionLog(None),
        client_factory=fake_factory(script),
    )
    assert result.is_error is True
    assert result.error == "the session ended with an error (error_max_turns)"
    assert result.structured is None
    assert result.text == "partial"


async def test_the_log_is_sent_every_interval(tmp_path: Path, manager: FakeManager) -> None:
    options = options_for(tmp_path, manager, time.monotonic() + 60)

    async def pause(_: FakeSDKClient) -> None:
        await asyncio.sleep(0.15)

    script = [tool_use("Read", {"file_path": "a"}), pause, tool_use("Grep", {"pattern": "b"})]
    log = session.SessionLog(manager.client())
    await session.run(
        options,
        "go",
        time.monotonic() + 30,
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
