"""End-to-end tests of the worker flow: the fake manager, a local bare repository, the fake SDK."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock
from conftest import (
    GIT_TOKEN,
    MANAGER_URL,
    PROXY_URL,
    TASK_ID,
    TASK_TOKEN,
    FakeManager,
    FakeSDKClient,
    fake_factory,
    make_brief,
    remote_head,
    run_git,
    sleep_forever,
)
from joshua_developer_worker import main, session
from joshua_developer_worker.manager_client import ManagerClient

BRANCH = "developer/task-0001"


def environ(home: Path) -> dict[str, str]:
    return {
        "TASK_ID": TASK_ID,
        "MANAGER_URL": MANAGER_URL,
        "TASK_TOKEN": TASK_TOKEN,
        "GIT_PROXY_URL": PROXY_URL,
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
    }


def result(structured: dict[str, Any] | None = None, **overrides: Any) -> ResultMessage:
    fields: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 10,
        "duration_api_ms": 8,
        "is_error": False,
        "num_turns": 4,
        "session_id": "s1",
        "total_cost_usd": 0.5,
        "usage": {"input_tokens": 1200, "output_tokens": 300},
        "result": "done",
        "structured_output": structured,
    }
    fields.update(overrides)
    return ResultMessage(**fields)


def write_file(name: str, commit: bool) -> Any:
    """A script step: the model writes ``name`` and, when ``commit``, commits it."""

    async def act(client: FakeSDKClient) -> AssistantMessage:
        cwd = Path(client.options.cwd)
        (cwd / name).write_text("hello\n")
        if commit:
            run_git("add", name, cwd=cwd)
            # The worker's git config holds the person's identity.
            run_git(
                "-c",
                "user.name=Alex Example",
                "-c",
                "user.email=alex@example.test",
                "commit",
                "-q",
                "-m",
                f"feat: add {name}",
                cwd=cwd,
            )
        block = ToolUseBlock(id="t1", name="Write", input={"file_path": name})
        return AssistantMessage(content=[block], model="m")

    return act


async def run(
    tmp_path: Path, home: Path, manager: FakeManager, script: list[Any], **kwargs: Any
) -> tuple[int, list[FakeSDKClient]]:
    made: list[FakeSDKClient] = []
    code = await main.run_task(
        environ(home),
        workdir=tmp_path / "work",
        manager=kwargs.pop("client", None) or manager.client(),
        client_factory=fake_factory(script, made),
    )
    return code, made


async def test_success_pushes_the_branch_and_reports(
    tmp_path: Path, home: Path, bare_repo: Path, manager: FakeManager
) -> None:
    manager.brief = make_brief(bare_repo)
    structured = {
        "summary": "Added hello.txt.",
        "files_changed": ["hello.txt"],
        "tests_run": "none: the project has no tests",
        "blocked": None,
    }
    code, made = await run(
        tmp_path, home, manager, [write_file("hello.txt", commit=True), result(structured)]
    )
    assert code == 0
    assert manager.running
    [report] = manager.reports
    assert report["status"] == "success"
    assert report["pushed"] is True
    assert report["head"] == BRANCH
    # The worker made the branch from main. The manager scans from clone_head.
    assert report["created_branch"] is True
    assert report["clone_head"] == remote_head(bare_repo, "main")
    # The manager takes the branch and the pull request from its own data.
    assert not {"branch", "pr_url", "pr_number"} & report.keys()
    assert report["commit_hash"] == remote_head(bare_repo, BRANCH)
    assert report["summary"] == "Added hello.txt."
    assert report["files_changed"] == ["hello.txt"]
    assert report["tests_run"] == ["none: the project has no tests"]
    assert (report["input_tokens"], report["output_tokens"]) == (1200, 300)
    assert report["estimated_cost"] == 0.5
    assert "tool Write" in report["log"]
    author = run_git("log", "-1", "--format=%an <%ae>", BRANCH, cwd=bare_repo).strip()
    assert author == "Alex Example <alex@example.test>"
    options = made[0].options
    assert options.env["ANTHROPIC_BASE_URL"] == f"{MANAGER_URL}/worker/claude"
    assert "Add a greeting file" in options.system_prompt


async def test_claude_md_reaches_the_prompt(
    tmp_path: Path, home: Path, bare_repo: Path, manager: FakeManager
) -> None:
    seed = tmp_path / "seed"
    run_git("checkout", "-q", "main", cwd=seed)
    (seed / "CLAUDE.md").write_text("Run make check.\n")
    run_git("add", "CLAUDE.md", cwd=seed)
    run_git("commit", "-q", "-m", "notes", cwd=seed)
    run_git("push", "-q", "origin", "main", cwd=seed)
    manager.brief = make_brief(bare_repo)
    code, made = await run(tmp_path, home, manager, [result({"summary": "No change."})])
    assert code == 0
    assert "Repository notes (CLAUDE.md, as text)" in made[0].options.system_prompt
    assert "Run make check." in made[0].options.system_prompt
    [report] = manager.reports
    assert report["status"] == "success"
    assert report["pushed"] is False and report["head"] is None
    assert report["commit_hash"] is None


async def test_blocked_reports_the_open_question(
    tmp_path: Path, home: Path, bare_repo: Path, manager: FakeManager
) -> None:
    manager.brief = make_brief(bare_repo)
    structured = {
        "summary": "Wrote the handler.",
        "files_changed": [],
        "tests_run": "",
        "blocked": "Which port does the service use?",
    }
    code, _ = await run(
        tmp_path, home, manager, [write_file("handler.py", commit=False), result(structured)]
    )
    assert code == 0
    [report] = manager.reports
    assert report["status"] == "blocked"
    assert report["open_question"] == "Which port does the service use?"
    assert report["summary"].startswith("Stopped: blocked on a question. No work was pushed.")
    assert report["pushed"] is False and report["head"] is None
    assert report["files_changed"] == []
    assert "Left uncommitted and not pushed: handler.py." in report["summary"]


async def test_the_ask_tool_in_the_flow(
    tmp_path: Path,
    home: Path,
    bare_repo: Path,
    manager: FakeManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    askers: list[session.Asker] = []
    real = session.ask_server

    def spy(asker: session.Asker) -> Any:
        askers.append(asker)
        return real(asker)

    monkeypatch.setattr(session, "ask_server", spy)

    async def ask(_: FakeSDKClient) -> AssistantMessage:
        reply = await askers[0]({"question": "Which port?"})
        assert reply["content"][0]["text"] == "Use port 8080."
        block = ToolUseBlock(id="t2", name="mcp__manager__ask", input={"question": "Which port?"})
        return AssistantMessage(content=[block], model="m")

    manager.brief = make_brief(bare_repo)
    code, _ = await run(tmp_path, home, manager, [ask, result({"summary": "Asked."})])
    assert code == 0
    assert manager.questions == ["Which port?"]
    assert "mcp__manager__ask" in manager.reports[0]["log"]


async def test_an_answer_after_the_old_deadline_still_finishes_success(
    tmp_path: Path,
    home: Path,
    bare_repo: Path,
    manager: FakeManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    askers: list[session.Asker] = []
    real = session.ask_server
    monkeypatch.setattr(session, "ask_server", lambda a: askers.append(a) or real(a))
    # The answer comes after 10 polls of 0.25 s: 2.5 s of wall time.
    manager.answer_after_polls = 10
    manager.poll_delay_s = 0.25

    async def ask(_: FakeSDKClient) -> AssistantMessage:
        asker = askers[0]
        reply = await asker({"question": "Which port?"})
        assert reply["content"][0]["text"] == "Use port 8080."
        # A clock that counted the wait would have run out by now.
        assert time.monotonic() - asker.clock.start > asker.clock.budget_s
        assert asker.clock.remaining() > 0
        block = ToolUseBlock(id="t2", name="mcp__manager__ask", input={"question": "Which port?"})
        return AssistantMessage(content=[block], model="m")

    # timeout_s 92: 2 s of work after the margin of 90 s.
    manager.brief = make_brief(
        bare_repo, persona={"name": "opus", "model": "m", "max_turns": 5, "timeout_s": 92}
    )
    structured = {
        "summary": "Added hello.txt on port 8080.",
        "files_changed": ["hello.txt"],
        "tests_run": "",
        "blocked": None,
    }
    code, made = await run(
        tmp_path,
        home,
        manager,
        [ask, write_file("hello.txt", commit=True), result(structured)],
    )
    assert code == 0
    assert not made[0].interrupted
    [report] = manager.reports
    assert report["status"] == "success"
    assert report["pushed"] is True
    assert manager.stops == 0


async def test_timeout_pushes_nothing_the_model_did_not_commit(
    tmp_path: Path, home: Path, bare_repo: Path, manager: FakeManager
) -> None:
    persona = {"name": "sonnet", "model": "m", "max_turns": 5, "timeout_s": 91}
    manager.brief = make_brief(bare_repo, persona=persona)
    code, made = await run(
        tmp_path, home, manager, [write_file("half.txt", commit=False), sleep_forever]
    )
    assert code == 0
    assert made[0].interrupted
    [report] = manager.reports
    assert report["status"] == "timed_out"
    assert report["pushed"] is False and report["head"] is None
    assert report["summary"].startswith("Stopped: the time limit ran out. No work was pushed.")
    assert "Left uncommitted and not pushed: half.txt." in report["summary"]
    assert report["commit_hash"] is None


async def test_timeout_while_a_question_is_open_is_blocked(
    tmp_path: Path,
    home: Path,
    bare_repo: Path,
    manager: FakeManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    askers: list[session.Asker] = []
    real = session.ask_server
    monkeypatch.setattr(session, "ask_server", lambda a: askers.append(a) or real(a))
    manager.answer_after_polls = 10_000
    manager.poll_delay_s = 0.01

    async def ask(_: FakeSDKClient) -> None:
        await askers[0]({"question": "Which port?"})

    manager.brief = make_brief(
        bare_repo,
        persona={"name": "opus", "model": "m", "max_turns": 5, "timeout_s": 91},
        ask_wait_s=0.2,
    )
    code, _ = await run(tmp_path, home, manager, [ask, sleep_forever])
    assert code == 0
    [report] = manager.reports
    assert report["status"] == "blocked"
    assert report["open_question"] == "Which port?"
    # The wait limit passed: the worker told the manager it stopped waiting.
    assert manager.stops == 1
    assert report["pushed"] is False and report["head"] is None


async def test_session_error_fails_and_pushes_nothing_uncommitted(
    tmp_path: Path, home: Path, bare_repo: Path, manager: FakeManager
) -> None:
    manager.brief = make_brief(bare_repo)
    code, _ = await run(
        tmp_path,
        home,
        manager,
        [write_file("partial.txt", commit=False), RuntimeError("the CLI stopped")],
    )
    assert code == 0
    [report] = manager.reports
    assert report["status"] == "failed"
    assert "RuntimeError" in report["error"]
    assert report["pushed"] is False and report["head"] is None
    assert report["commit_hash"] is None
    assert "partial.txt" in report["summary"]


async def test_a_push_failure_fails_the_task(
    tmp_path: Path, home: Path, bare_repo: Path, manager: FakeManager
) -> None:
    async def break_remote(client: FakeSDKClient) -> None:
        missing = tmp_path / "missing" / GIT_TOKEN
        run_git("remote", "set-url", "origin", str(missing), cwd=Path(client.options.cwd))

    manager.brief = make_brief(bare_repo)
    code, _ = await run(
        tmp_path,
        home,
        manager,
        [write_file("a.txt", commit=True), break_remote, result({"summary": "Done."})],
    )
    assert code == 0
    [report] = manager.reports
    assert report["status"] == "failed"
    assert report["pushed"] is False and report["head"] is None
    assert report["error"].startswith(f"the push of {BRANCH} failed")
    assert GIT_TOKEN not in report["error"] and GIT_TOKEN not in report["log"]


async def test_rework_uses_the_existing_branch(
    tmp_path: Path, home: Path, bare_repo: Path, manager: FakeManager
) -> None:
    before = remote_head(bare_repo, "feature")
    manager.brief = make_brief(bare_repo, task_type="rework", branch="feature", feedback="More.")
    code, _ = await run(
        tmp_path, home, manager, [write_file("more.txt", commit=True), result({"summary": "More."})]
    )
    assert code == 0
    [report] = manager.reports
    assert report["status"] == "success"
    assert report["pushed"] is True and report["head"] == "feature"
    assert report["created_branch"] is False
    assert report["clone_head"] == before
    assert run_git("log", "-1", "--format=%s", "feature", cwd=bare_repo).strip() == (
        "feat: add more.txt"
    )


async def test_a_rework_on_a_missing_branch_fails_before_the_session(
    tmp_path: Path, home: Path, bare_repo: Path, manager: FakeManager
) -> None:
    manager.brief = make_brief(bare_repo, task_type="rework", branch="gone")
    code, made = await run(tmp_path, home, manager, [])
    assert code == 0
    assert made == []
    [report] = manager.reports
    assert report["status"] == "failed"
    assert "no branch gone" in report["error"]
    assert report["pushed"] is False and report["head"] is None


async def test_a_resumed_task_takes_the_pushed_branch(
    tmp_path: Path, home: Path, bare_repo: Path, manager: FakeManager
) -> None:
    before = remote_head(bare_repo, "feature")
    manager.brief = make_brief(
        bare_repo,
        branch="feature",
        resumed=True,
        note="Stopped: blocked on a question.",
        answer="Use port 8080.",
    )
    code, made = await run(
        tmp_path, home, manager, [write_file("more.txt", commit=True), result({"summary": "Ok."})]
    )
    assert code == 0
    [report] = manager.reports
    assert report["status"] == "success" and report["pushed"] is True
    # The work continues on top of the pushed branch.
    parent = run_git("rev-parse", "feature~1", cwd=bare_repo).strip()
    assert parent == before
    system = made[0].options.system_prompt
    assert "You are resuming a task. A previous worker stopped." in system
    assert "The answer to its question: Use port 8080." in system


async def test_a_resumed_task_never_creates_its_branch(
    tmp_path: Path, home: Path, bare_repo: Path, manager: FakeManager
) -> None:
    manager.brief = make_brief(bare_repo, branch="gone", resumed=True)
    code, made = await run(tmp_path, home, manager, [])
    assert code == 0
    assert made == []
    [report] = manager.reports
    assert report["status"] == "failed"
    assert "no branch gone" in report["error"]
    assert remote_head(bare_repo, "gone") is None


async def test_a_person_with_no_git_identity_fails(
    tmp_path: Path, home: Path, bare_repo: Path, manager: FakeManager
) -> None:
    manager.brief = make_brief(bare_repo, git={"name": None, "email": None})
    code, _ = await run(tmp_path, home, manager, [])
    assert code == 0
    [report] = manager.reports
    assert report["status"] == "failed" and "git name" in report["error"]
    assert report["pushed"] is False


async def test_brief_503_reports_failed(tmp_path: Path, home: Path, manager: FakeManager) -> None:
    manager.brief_status = 503
    code, made = await run(tmp_path, home, manager, [])
    assert code == 0
    assert made == []
    [report] = manager.reports
    assert report["status"] == "failed"
    assert report["error"] == "the manager refused the brief: token_missing"
    assert report["pushed"] is False and report["head"] is None


async def test_brief_409_exits_0_with_no_report(
    tmp_path: Path, home: Path, manager: FakeManager
) -> None:
    manager.ended = True
    code, made = await run(tmp_path, home, manager, [])
    assert code == 0
    assert made == [] and manager.reports == []


async def test_an_unreachable_manager_exits_1(tmp_path: Path, home: Path) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = ManagerClient(
        MANAGER_URL, TASK_ID, TASK_TOKEN, transport=httpx.MockTransport(refuse), backoff_s=0
    )
    code, _ = await run(tmp_path, home, FakeManager(), [], client=client)
    assert code == 1


async def test_a_report_the_manager_refuses_exits_1(
    tmp_path: Path, home: Path, bare_repo: Path, manager: FakeManager
) -> None:
    manager.brief = make_brief(bare_repo)
    manager.report_status = 500
    code, _ = await run(tmp_path, home, manager, [result({"summary": "x"})])
    assert code == 1


async def test_a_task_that_ends_during_the_run_exits_0(
    tmp_path: Path, home: Path, bare_repo: Path, manager: FakeManager
) -> None:
    manager.brief = make_brief(bare_repo)

    async def end(_: FakeSDKClient) -> None:
        manager.ended = True

    code, _ = await run(tmp_path, home, manager, [end, result({"summary": "x"})])
    assert code == 0
    assert manager.reports == []


async def test_an_unexpected_failure_is_reported(
    tmp_path: Path,
    home: Path,
    bare_repo: Path,
    manager: FakeManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise ValueError(f"bad {GIT_TOKEN}")

    monkeypatch.setattr(main.prompt, "compose", boom)
    manager.brief = make_brief(bare_repo)
    code, _ = await run(tmp_path, home, manager, [])
    assert code == 0
    [report] = manager.reports
    assert report["status"] == "failed"
    assert "ValueError" in report["error"] and GIT_TOKEN not in report["error"]


async def test_a_missing_environment_exits_1(tmp_path: Path) -> None:
    assert await main.run_task({"TASK_ID": TASK_ID}, workdir=tmp_path) == 1


def test_read_env_defaults() -> None:
    env = main.read_env({"TASK_ID": "t", "MANAGER_URL": "http://manager.test", "TASK_TOKEN": "k"})
    assert env is not None
    assert env.home == Path("/work/home")
    assert env.config_dir == Path("/work/home/.claude")
    assert env.proxy_url is None


def test_read_claude_md_ignores_a_symlink(tmp_path: Path) -> None:
    (tmp_path / "secret").write_text("x")
    (tmp_path / "CLAUDE.md").symlink_to(tmp_path / "secret")
    assert main.read_claude_md(tmp_path) is None
    assert main.read_claude_md(tmp_path / "none") is None


def test_main_runs_the_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TASK_ID", raising=False)
    import logging

    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        assert main.main() == 1
    finally:
        root.handlers[:] = saved


def test_no_test_names_a_real_host() -> None:
    hosts: set[str] = set()
    for path in Path(__file__).parent.glob("*.py"):
        hosts |= set(re.findall(r"https?://(?:[^@/\s\"']+@)?([A-Za-z0-9.-]+)", path.read_text()))
    assert hosts
    assert all(host.endswith((".test", ".invalid")) for host in hosts), hosts


def test_the_stop_note_says_how_long_the_worker_waited() -> None:
    assert main._stop_note("the time limit ran out", False, BRANCH) == (
        "Stopped: the time limit ran out. No work was pushed."
    )
    assert main._stop_note("blocked on a question", True, BRANCH, 7200.0) == (
        f"Stopped: blocked on a question. The work so far is on branch {BRANCH}. "
        "Waited 120 minutes for an answer."
    )
