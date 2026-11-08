"""The Claude Code session of the worker. The one module that imports the SDK.

The session gets the file tools and Bash, and one MCP server: ``manager``,
in-process, with the one tool ``ask``. It loads no settings from the
checkout (``setting_sources=[]``) and no MCP config but its own. It never
holds WebSearch or WebFetch; ``check_options`` refuses options that do.

The session runs on a ``Clock``. A watchdog interrupts the session when the
clock has no time left. The clock pauses while ``ask`` waits for an answer.

A test aid, not for production: ``JOSHUA_WORKER_FAKE_SESSION=1`` makes
``run`` skip the SDK and run a scripted session. The scripted session writes
``hello-from-worker.txt`` with the task id, commits it as the person, and
returns a structured result. With ``JOSHUA_WORKER_FAKE_SESSION=ask`` it first
calls ``ask`` once with ``FAKE_QUESTION`` and writes the reply into the file.
It needs no Claude token. A compose end-to-end test uses it to prove the
Docker runtime without a call to Claude.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    McpSdkServerConfig,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

from joshua_developer_worker import git
from joshua_developer_worker.clock import Clock
from joshua_developer_worker.git import scrub
from joshua_developer_worker.log import get_logger
from joshua_developer_worker.manager_client import (
    ManagerClient,
    ManagerError,
    NotSent,
    TaskEnded,
)

logger = get_logger("joshua_developer_worker.session")

BASE_TOOLS = ("Read", "Edit", "Write", "Bash", "Glob", "Grep")
ASK_SERVER = "manager"
ASK_TOOL = f"mcp__{ASK_SERVER}__ask"
FORBIDDEN_TOOLS = frozenset({"WebSearch", "WebFetch"})
RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "tests_run": {"type": "string"},
        "blocked": {"type": ["string", "null"]},
    },
    "required": ["summary", "files_changed", "tests_run", "blocked"],
    "additionalProperties": False,
}
NOBODY = (
    "Nobody can be reached to answer this. Do the parts of the task that do not need "
    "the answer, and set `blocked` with your question in the final result."
)
ASK_DESCRIPTION = (
    "Ask the person's assistant one clear question, for a fact that is not in the "
    "repository. The answer can take minutes."
)
DEFAULT_ASK_WAIT_S = 7200.0
LOG_FLUSH_S = 30.0
# The watchdog checks the clock this often.
WATCH_S = 1.0
MAX_TOOL_INPUT = 200
MAX_LOG_LINE = 1000
STOP_TIMEOUT_S = 15.0
MAX_BUFFER_SIZE = 32 * 1024 * 1024

# The scripted session, a test aid. See the module docstring.
FAKE_SESSION_ENV = "JOSHUA_WORKER_FAKE_SESSION"
FAKE_SESSION_MODES = ("1", "ask")
FAKE_FILE = "hello-from-worker.txt"
FAKE_QUESTION = "Which greeting goes in hello-from-worker.txt?"

# Switches that turn off what the Claude CLI sends to other hosts than
# ANTHROPIC_BASE_URL. With `network: off`, such a call fails anyway.
QUIET_ENV = {
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}


def cli_env(manager_url: str, task_token: str, home: Path, config_dir: Path) -> dict[str, str]:
    """The environment the SDK gives the Claude CLI.

    The CLI sends its requests to the forwarder on the manager, with the task
    token as its Claude token. The manager puts the real token on them.
    """
    return {
        "ANTHROPIC_BASE_URL": f"{manager_url.rstrip('/')}/worker/claude",
        "CLAUDE_CODE_OAUTH_TOKEN": task_token,
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(config_dir),
        **QUIET_ENV,
    }


def _text(text: str, is_error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["is_error"] = True
    return out


def duration(seconds: float) -> str:
    """``seconds`` as words: ``45 seconds``, ``1 minute``, ``120 minutes``."""
    if seconds < 60:
        count, unit = round(seconds), "second"
    else:
        count, unit = round(seconds / 60), "minute"
    return f"{count} {unit}" if count == 1 else f"{count} {unit}s"


def no_answer(waited_s: float) -> str:
    """The tool reply when the wait limit passes with no answer."""
    return (
        f"No answer arrived in {duration(waited_s)}. "
        "Commit your work and report what you still need."
    )


def no_wait_left(total_s: float) -> str:
    """The tool reply when the task has used all of its wait time."""
    return (
        f"This task has used all of its {duration(total_s)} of wait time for answers, "
        "so the question was not sent. Commit your work and report what you still need."
    )


class Asker:
    """The handler of the ``ask`` tool. It keeps the question that has no answer.

    While it waits for an answer, the task clock is paused. ``ask_wait_s`` is
    the total wall time of all the waits of the task: each wait stops when
    the total is used. The manager's hard cap on the worker has room for this
    total and no more.
    """

    def __init__(
        self, client: ManagerClient, clock: Clock, ask_wait_s: float = DEFAULT_ASK_WAIT_S
    ) -> None:
        self.client = client
        self.clock = clock
        self.ask_wait_s = ask_wait_s
        self.open_question: str | None = None
        self.asked = 0
        # The seconds of all the waits so far, in wall time.
        self.waited_s = 0.0

    async def __call__(self, args: dict[str, Any]) -> dict[str, Any]:
        question = str(args.get("question") or "").strip()
        if not question:
            return _text("Give one clear question.", is_error=True)
        self.open_question = question
        self.asked += 1
        logger.info({"message": "ask", "task_id": self.client.task_id, "count": self.asked})
        left = self.ask_wait_s - self.waited_s
        if left <= 0:
            logger.info({"message": "ask wait time used", "task_id": self.client.task_id})
            with contextlib.suppress(TaskEnded, ManagerError, httpx.HTTPError):
                await self.client.stop_ask()
            return _text(no_wait_left(self.ask_wait_s))
        self.clock.pause()
        started = time.monotonic()
        try:
            answer = await self.client.ask(question, left)
        except TaskEnded:
            return _text("The task ended. Stop now.", is_error=True)
        except NotSent as exc:
            logger.info(
                {"message": "ask not sent", "task_id": self.client.task_id, "reason": exc.reason}
            )
            return _text(NOBODY)
        except (ManagerError, httpx.HTTPError):
            return _text(
                "The question could not be sent. Do the parts that do not depend on it "
                "and set blocked.",
                is_error=True,
            )
        finally:
            waited = time.monotonic() - started
            self.waited_s += waited
            self.clock.resume()
        if answer is None:
            logger.info({"message": "ask wait limit passed", "task_id": self.client.task_id})
            with contextlib.suppress(TaskEnded, ManagerError, httpx.HTTPError):
                await self.client.stop_ask()
            return _text(no_answer(waited))
        self.open_question = None
        return _text(answer)


def ask_server(asker: Asker) -> McpSdkServerConfig:
    """The in-process MCP server ``manager`` with its one tool, ``ask``."""
    ask_tool = tool("ask", ASK_DESCRIPTION, {"question": str})(asker)
    return create_sdk_mcp_server(name=ASK_SERVER, tools=[ask_tool])


def check_options(options: ClaudeAgentOptions) -> None:
    """Refuse options that give the session the web, or settings from the checkout."""
    tools = set(options.tools or []) | set(options.allowed_tools)
    if tools & FORBIDDEN_TOOLS:
        raise AssertionError(f"the worker may not hold {sorted(tools & FORBIDDEN_TOOLS)}")
    if not isinstance(options.tools, list) or set(options.tools) - set(BASE_TOOLS):
        raise AssertionError("the worker holds only the file tools and Bash")
    if options.setting_sources != []:
        raise AssertionError("the worker loads no settings: setting_sources must be []")


def build_options(
    system_prompt: str,
    cwd: Path,
    model: str | None,
    max_turns: int,
    ask_server: McpSdkServerConfig,
    *,
    effort: str | None = None,
    env: dict[str, str] | None = None,
    stderr: Callable[[str], None] | None = None,
) -> ClaudeAgentOptions:
    """The SDK options of the session."""
    kwargs: dict[str, Any] = {
        "system_prompt": system_prompt,
        "cwd": str(cwd),
        "tools": list(BASE_TOOLS),
        "allowed_tools": [*BASE_TOOLS, ASK_TOOL],
        "disallowed_tools": sorted(FORBIDDEN_TOOLS),
        "mcp_servers": {ASK_SERVER: ask_server},
        "strict_mcp_config": True,
        "permission_mode": "bypassPermissions",
        "setting_sources": [],
        "max_turns": max_turns,
        "output_format": {"type": "json_schema", "schema": RESULT_SCHEMA},
        "env": dict(env or {}),
        "max_buffer_size": MAX_BUFFER_SIZE,
    }
    if model:
        kwargs["model"] = model
    if effort:
        kwargs["effort"] = effort
    if stderr is not None:
        kwargs["stderr"] = stderr
    options = ClaudeAgentOptions(**kwargs)
    check_options(options)
    return options


class SessionLog:
    """The session log: one line per tool call, sent to the manager in chunks."""

    def __init__(self, client: ManagerClient | None) -> None:
        self.client = client
        self.lines: list[str] = []
        self._sent = 0
        self._closed = False

    def add(self, line: str) -> None:
        self.lines.append(scrub(line)[:MAX_LOG_LINE])

    def text(self) -> str:
        return "\n".join(self.lines)

    async def flush(self) -> None:
        """Send the lines the manager does not have yet."""
        if self.client is None or self._closed or self._sent >= len(self.lines):
            return
        end = len(self.lines)
        chunk = "\n".join(self.lines[self._sent : end]) + "\n"
        try:
            await self.client.log(chunk)
        except TaskEnded:
            self._closed = True
            return
        except (ManagerError, httpx.HTTPError) as exc:
            logger.warning(
                {"message": "the session log was not sent", "error_type": type(exc).__name__}
            )
            return
        self._sent = end


@dataclass
class SessionResult:
    """What one session gave."""

    text: str = ""
    structured: dict[str, Any] | None = None
    is_error: bool = False
    timed_out: bool = False
    error: str | None = None
    usage: dict[str, Any] | None = None
    cost_usd: float | None = None
    tool_calls: list[dict[str, str]] = field(default_factory=list)


def parse_structured(value: Any) -> dict[str, Any] | None:
    """The structured result, with its four fields, or None."""
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped.startswith("{"):
            return None
        try:
            value = json.loads(stripped)
        except ValueError:
            return None
    if not isinstance(value, dict) or not isinstance(value.get("summary"), str):
        return None
    files = value.get("files_changed")
    blocked = value.get("blocked")
    return {
        "summary": value["summary"],
        "files_changed": [str(f) for f in files] if isinstance(files, list) else [],
        "tests_run": str(value.get("tests_run") or ""),
        "blocked": (blocked.strip() or None) if isinstance(blocked, str) else None,
    }


async def _consume(client: Any, prompt: str, result: SessionResult, log: SessionLog) -> None:
    await client.connect()
    await client.query(prompt)
    parts: list[str] = []
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    raw = json.dumps(block.input, default=str, ensure_ascii=False)
                    record = {"name": block.name, "input": raw[:MAX_TOOL_INPUT]}
                    result.tool_calls.append(record)
                    log.add(f"tool {record['name']} {record['input']}")
                elif isinstance(block, TextBlock):
                    parts.append(block.text)
        elif isinstance(message, ResultMessage):
            result.is_error = bool(message.is_error)
            result.usage = message.usage
            result.cost_usd = message.total_cost_usd
            result.text = (message.result or "").strip()
            result.structured = parse_structured(message.structured_output) or parse_structured(
                message.result
            )
            if message.is_error:
                result.error = f"the session ended with an error ({message.subtype})"
            log.add(f"result {message.subtype} turns={message.num_turns}")
    if not result.text:
        result.text = "".join(parts).strip()


async def _bounded(awaitable: Any) -> None:
    try:
        await asyncio.wait_for(awaitable, STOP_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — stopping the CLI is best effort
        logger.warning({"message": "the session did not stop cleanly", "error": type(exc).__name__})


async def _flush_every(log: SessionLog, every_s: float) -> None:
    while True:
        await asyncio.sleep(every_s)
        await log.flush()


async def _watchdog(work: Awaitable[None], clock: Clock, watch_s: float) -> None:
    """Run ``work`` until it ends, or until ``clock`` has no time left.

    The watchdog checks the clock every ``watch_s`` seconds, or sooner when
    less time is left. A paused clock does not run out. Raises TimeoutError,
    after it cancels ``work``, when the time is used.
    """
    task = asyncio.ensure_future(work)
    try:
        while True:
            remaining = clock.remaining()
            if remaining <= 0:
                raise TimeoutError
            done, _ = await asyncio.wait({task}, timeout=min(watch_s, remaining))
            if done:
                task.result()
                return
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def fake_mode(environ: Mapping[str, str]) -> str | None:
    """The scripted session mode in ``environ``: ``1``, ``ask``, or None for a real session."""
    value = (environ.get(FAKE_SESSION_ENV) or "").strip()
    if value and value not in FAKE_SESSION_MODES:
        logger.warning({"message": f"{FAKE_SESSION_ENV} is not 1 or ask; a real session runs"})
        return None
    return value or None


async def run_scripted(mode: str, cwd: Path, asker: Asker | None, log: SessionLog) -> SessionResult:
    """The scripted session of the test aid. It never calls Claude.

    It writes ``FAKE_FILE`` and commits it with the identity in git's global
    config, which the worker set from the brief. The commit is the
    session's own, as a model's commit is, so the worker still makes none.
    """
    task_id = asker.client.task_id if asker is not None else "unknown"
    logger.warning({"message": "scripted session: a test aid, not Claude", "task_id": task_id})
    lines = [f"task {task_id}"]
    blocked: str | None = None
    if mode == "ask":
        if asker is None:
            return SessionResult(is_error=True, error="the scripted ask session needs the ask tool")
        reply = await asker({"question": FAKE_QUESTION})
        log.add(f"tool {ASK_TOOL} {json.dumps({'question': FAKE_QUESTION})}")
        lines.append(f"answer: {reply['content'][0]['text']}")
        blocked = asker.open_question
    (cwd / FAKE_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.add(f"tool Write {json.dumps({'file_path': FAKE_FILE})}")
    try:
        git._run(["add", FAKE_FILE], cwd=cwd)
        git._run(["commit", "--quiet", "-m", f"test: add {FAKE_FILE}"], cwd=cwd)
    except git.GitError as exc:
        log.add(f"session failed: {exc}")
        await log.flush()
        return SessionResult(is_error=True, error=str(exc)[:500])
    summary = f"Scripted session: wrote {FAKE_FILE} for task {task_id}."
    log.add("result success turns=1")
    await log.flush()
    return SessionResult(
        text=summary,
        structured={
            "summary": summary,
            "files_changed": [FAKE_FILE],
            "tests_run": "none: scripted session",
            "blocked": blocked,
        },
    )


async def run(
    options: ClaudeAgentOptions,
    prompt: str,
    clock: Clock,
    *,
    log: SessionLog,
    client_factory: Callable[..., Any] = ClaudeSDKClient,
    flush_s: float = LOG_FLUSH_S,
    watch_s: float = WATCH_S,
    asker: Asker | None = None,
    fake: str | None = None,
) -> SessionResult:
    """Run one session until it ends or ``clock`` has no time left.

    ``fake`` (``1`` or ``ask``) runs ``run_scripted`` instead of the SDK. It
    is a test aid. When it is None, the SDK session runs.
    """
    check_options(options)
    if fake:
        return await run_scripted(fake, Path(str(options.cwd)), asker, log)
    result = SessionResult()
    client = client_factory(options=options)
    flusher = asyncio.create_task(_flush_every(log, flush_s))
    try:
        if clock.remaining() <= 0:
            raise TimeoutError
        await _watchdog(_consume(client, prompt, result, log), clock, watch_s)
    except TimeoutError:
        result.timed_out = True
        log.add("session stopped: the deadline passed")
        await _bounded(client.interrupt())
    except Exception as exc:  # noqa: BLE001 — every failure becomes a report
        result.is_error = True
        result.error = scrub(f"{type(exc).__name__}: {exc}")[:500]
        log.add(f"session failed: {result.error}")
    finally:
        flusher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await flusher
        await _bounded(client.disconnect())
        await log.flush()
    return result
