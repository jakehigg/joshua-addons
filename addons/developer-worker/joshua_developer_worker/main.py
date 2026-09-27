"""The flow of one worker, from the brief to the report.

1. Get the brief from the manager.
2. Configure git, clone the repository, and check out the branch.
3. Read ``CLAUDE.md`` of the checkout as text, if there is one.
4. Tell the manager the session runs, and run the session until it ends or
   the deadline passes: the persona timeout minus ``DEADLINE_MARGIN_S``.
5. Always: commit what is not committed, push the branch when it has new
   commits, and send the report.

The exit code is 0 when the manager accepted the report, or when the task
had already ended. It is 1 only when the report could not be sent.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from joshua_developer_worker import git, log, prompt, session
from joshua_developer_worker.manager_client import ManagerClient, ManagerError, Report, TaskEnded

logger = log.get_logger("joshua_developer_worker.main")

WORKDIR = Path("/work")
CHECKOUT = "repo"
DEFAULT_TIMEOUT_S = 2400
DEFAULT_MAX_TURNS = 80
# The session stops this long before the persona timeout, so the commit, the
# push, and the report fit before the manager kills the container.
DEADLINE_MARGIN_S = 90
MAX_REPORT_LOG = 64_000
MAX_SUMMARY = 8_000
START_PROMPT = "Do the task in your instructions. End with the structured result."


@dataclass(frozen=True)
class WorkerEnv:
    """What the worker takes from its environment."""

    task_id: str
    manager_url: str
    token: str
    proxy_url: str | None
    home: Path
    config_dir: Path


def read_env(environ: Mapping[str, str]) -> WorkerEnv | None:
    """The worker environment, or None when a required variable is missing."""
    task_id = environ.get("TASK_ID", "")
    manager_url = environ.get("MANAGER_URL", "")
    token = environ.get("TASK_TOKEN", "")
    if not (task_id and manager_url and token):
        return None
    home = Path(environ.get("HOME") or "/work/home")
    return WorkerEnv(
        task_id=task_id,
        manager_url=manager_url,
        token=token,
        proxy_url=environ.get("GIT_PROXY_URL") or None,
        home=home,
        config_dir=Path(environ.get("CLAUDE_CONFIG_DIR") or home / ".claude"),
    )


@contextlib.contextmanager
def step(name: str, task_id: str) -> Iterator[None]:
    """Log one JSON line for a step: its name, the task id, the duration, and the outcome."""
    started = time.monotonic()
    ok = True
    try:
        yield
    except BaseException:
        ok = False
        raise
    finally:
        logger.info(
            {
                "message": "step",
                "step": name,
                "task_id": task_id,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "ok": ok,
            }
        )


def read_claude_md(dest: Path) -> str | None:
    """``CLAUDE.md`` at the root of the checkout, as text, or None."""
    path = dest / "CLAUDE.md"
    if not path.is_file() or path.is_symlink():
        return None
    with path.open("rb") as handle:
        raw = handle.read(prompt.MAX_CLAUDE_MD * 4 + 4)
    return raw.decode("utf-8", "replace")


async def _send(client: ManagerClient, report: Report) -> int:
    with step("report", client.task_id):
        try:
            await client.report(report)
        except TaskEnded:
            logger.warning(
                {"message": "the task ended before the report", "task_id": client.task_id}
            )
            return 0
        except (ManagerError, httpx.HTTPError) as exc:
            logger.error(
                {
                    "message": "the report could not be sent",
                    "task_id": client.task_id,
                    "error_type": type(exc).__name__,
                }
            )
            return 1
    return 0


def _stop_note(reason: str, pushed: bool, branch: str) -> str:
    where = f"The work so far is on branch {branch}." if pushed else "No work was pushed."
    return f"Stopped: {reason}. {where}"


def finish(
    dest: Path,
    brief: dict[str, Any],
    clone_head: str,
    result: session.SessionResult,
    asker: session.Asker,
    log_: session.SessionLog,
) -> Report:
    """Commit, push, and build the report after the session."""
    branch = str(brief["branch"])
    structured = result.structured
    if result.timed_out:
        reason = "the time limit ran out"
    elif result.is_error:
        reason = "the session failed"
    elif structured and structured["blocked"]:
        reason = "blocked on a question"
    else:
        reason = "the session ended"

    error = result.error
    pushed = False
    push_failed = False
    new_head: str | None = clone_head
    try:
        if git.dirty(dest):
            git.commit_all(dest, f"wip: developer stopped ({reason})")
        new_head = git.head(dest)
        if new_head and new_head != clone_head:
            with step("push", asker.client.task_id):
                pushed, push_error = git.push(dest, branch)
            if not pushed:
                push_failed = True
                error = f"the push of {branch} failed: {push_error}"
                log_.add(error)
    except git.GitError as exc:
        push_failed = True
        error = f"the commit after the session failed: {exc}"
        log_.add(error)
    moved = bool(new_head) and new_head != clone_head

    open_question: str | None = None
    if result.timed_out:
        status = "timed_out"
        if asker.open_question:
            status = "blocked"
            open_question = asker.open_question
    elif result.is_error:
        status = "failed"
    elif structured and structured["blocked"]:
        status = "blocked"
        open_question = structured["blocked"]
    else:
        status = "success"
    if push_failed:
        status = "failed"

    body = structured["summary"] if structured else result.text
    if result.timed_out or status == "blocked":
        summary = _stop_note(reason, pushed, branch)
        if open_question:
            summary += f" Open question: {open_question}"
        if body:
            summary += f"\n\n{body}"
    else:
        summary = body
    files: list[str] = list(structured["files_changed"]) if structured else []
    if not files and moved:
        with contextlib.suppress(git.GitError):
            files = git.changed_files(dest, clone_head)
    tests = structured["tests_run"] if structured else ""
    usage = result.usage or {}
    return Report(
        status=status,
        summary=summary[:MAX_SUMMARY],
        files_changed=files,
        tests_run=[tests] if tests else [],
        open_question=open_question,
        branch=branch,
        pushed=pushed,
        head=branch if pushed else None,
        commit_hash=new_head if moved else None,
        error=error,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        estimated_cost=result.cost_usd,
        log=log_.text()[-MAX_REPORT_LOG:],
    )


async def _work(
    env: WorkerEnv,
    client: ManagerClient,
    brief: dict[str, Any],
    workdir: Path,
    started: float,
    client_factory: Callable[..., Any] | None,
) -> Report:
    task_id = env.task_id
    branch = str(brief["branch"])
    persona = brief.get("persona") or {}
    timeout_s = int(persona.get("timeout_s") or DEFAULT_TIMEOUT_S)
    deadline = started + timeout_s - DEADLINE_MARGIN_S
    dest = workdir / CHECKOUT
    log_ = session.SessionLog(client)

    person = brief.get("git") or {}
    if not person.get("name") or not person.get("email"):
        return Report(
            status="failed",
            branch=branch,
            error="the person has no git name or email; set them with set_settings",
        )
    try:
        with step("git_configure", task_id):
            git.configure(
                env.home,
                person["name"],
                person["email"],
                str(brief.get("git_username") or "git"),
                str(brief["git_token"]),
                urlsplit(str(brief["repo_url"])).netloc,
                env.proxy_url,
            )
        with step("clone", task_id):
            git.clone(str(brief["repo_url"]), dest)
        with step("checkout", task_id):
            clone_head = git.checkout(
                dest,
                branch,
                brief.get("base_branch"),
                # A rework, or a resumed task, works on a branch that exists.
                create_if_missing=brief.get("task_type") != "rework" and not brief.get("resumed"),
            )
    except git.GitError as exc:
        return Report(status="failed", branch=branch, error=str(exc), log=log_.text())

    claude_md = read_claude_md(dest)
    with step("running", task_id):
        await client.running()

    env.config_dir.mkdir(parents=True, exist_ok=True)
    asker = session.Asker(client, max(started, deadline - session.ASK_MARGIN_S))
    options = session.build_options(
        prompt.compose(brief, claude_md),
        dest,
        persona.get("model"),
        int(persona.get("max_turns") or DEFAULT_MAX_TURNS),
        session.ask_server(asker),
        effort=persona.get("effort"),
        env=session.cli_env(env.manager_url, env.token, env.home, env.config_dir),
        stderr=lambda line: log_.add(f"cli: {line}"),
    )
    kwargs: dict[str, Any] = {"log": log_}
    if client_factory is not None:
        kwargs["client_factory"] = client_factory
    with step("session", task_id):
        result = await session.run(options, START_PROMPT, deadline, **kwargs)
    with step("finish", task_id):
        return finish(dest, brief, clone_head, result, asker, log_)


async def run_task(
    environ: Mapping[str, str],
    *,
    workdir: Path = WORKDIR,
    manager: ManagerClient | None = None,
    client_factory: Callable[..., Any] | None = None,
) -> int:
    """Do the task of this worker. Returns the exit code."""
    started = time.monotonic()
    env = read_env(environ)
    if env is None:
        logger.error({"message": "TASK_ID, MANAGER_URL, or TASK_TOKEN is not set"})
        return 1
    git.register_secret(env.token)
    git.register_secret(env.proxy_url)
    client = manager or ManagerClient(env.manager_url, env.task_id, env.token)
    try:
        with step("brief", env.task_id):
            try:
                brief = await client.brief()
            except TaskEnded:
                logger.warning(
                    {"message": "the task ended before the brief", "task_id": env.task_id}
                )
                return 0
            except ManagerError as exc:
                return await _send(
                    client,
                    Report(status="failed", error=f"the manager refused the brief: {exc.reason}"),
                )
            except httpx.HTTPError as exc:
                logger.error(
                    {
                        "message": "the brief could not be read",
                        "task_id": env.task_id,
                        "error_type": type(exc).__name__,
                    }
                )
                return 1
        git.register_secret(str(brief.get("git_token") or ""))
        try:
            report = await _work(env, client, brief, workdir, started, client_factory)
        except TaskEnded:
            logger.warning(
                {"message": "the task ended while the worker ran", "task_id": env.task_id}
            )
            return 0
        except Exception as exc:  # noqa: BLE001 — every failure becomes a report
            logger.error(
                {
                    "message": "the worker failed",
                    "task_id": env.task_id,
                    "error_type": type(exc).__name__,
                }
            )
            report = Report(
                status="failed",
                branch=brief.get("branch"),
                error=git.scrub(f"the worker failed: {type(exc).__name__}: {exc}")[:500],
            )
        return await _send(client, report)
    finally:
        await client.aclose()


def main() -> int:
    """Run the worker with the process environment."""
    log.configure()
    return asyncio.run(run_task(os.environ))
