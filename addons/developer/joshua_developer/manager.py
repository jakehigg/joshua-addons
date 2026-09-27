"""The manager: the rules that start a task, record its report, and take an answer.

The MCP tools call this class. A call that breaks a rule gets a dict with
``status: "rejected"``, a ``reason``, and a ``message``. The reasons are
``invalid_arguments``, ``repo_not_allowed``, ``repo_not_configured``,
``token_missing``, ``pr_not_found``, ``pr_not_open``, ``platform_error``,
``unknown_persona``, ``concurrency_limit``, ``locked``, ``not_waiting``, and
``not_resumable``.

When a worker reports a push, the manager gates the pull request: it reads
the diff over the platform API and scans it for credentials. A hit deletes
the branch and fails the task. A clean diff gets a pull request.

The manager also serves the worker side: it mints the task token of each
task, takes a question from a worker, and wakes the worker that waits for
the answer. An answer to a task that stopped ``blocked`` or ``timed_out``
after a push starts a new worker for the same task, on the pushed branch.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from joshua_developer import notify, platforms
from joshua_developer.config import DeveloperConfig, Settings
from joshua_developer.locks import LockedError, LockManager, LockStolenError
from joshua_developer.log import get_logger
from joshua_developer.platforms import Platform, PlatformConfigError, PlatformError, Resolved
from joshua_developer.repos import (
    RepoError,
    brief_title,
    check_branch,
    default_branch_name,
    normalize_repo,
    parse_pr,
    repo_allowed,
)
from joshua_developer.runtime import Report, Runtime
from joshua_developer.scan import Finding, scan_diff
from joshua_developer.store import ACTIVE_STATUSES, TERMINAL_STATUSES, TaskStore, now_iso

logger = get_logger("joshua_developer.manager")

RESTART_ERROR = "manager restarted"
# The time a lock outlives the persona's timeout before a new task may take it.
LOCK_MARGIN_S = 600
MAX_TEXT = 100_000
MAX_QUESTION = 10_000
DEFAULT_BASE_BRANCH = "main"
# The states of a pull request that a rework cannot change.
CLOSED_PR_STATES = frozenset({"closed", "merged", "locked"})
# The states from which an answer resumes a task.
RESUMABLE_STATUSES = ("blocked", "timed_out")


def rejected(reason: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"status": "rejected", "reason": reason, "message": message, **extra}


async def retry_5xx[T](call: Callable[[], Awaitable[T]]) -> T:
    """Run ``call``. On a 5xx from the git host, run it one more time."""
    try:
        return await call()
    except PlatformError as exc:
        if exc.status < 500:
            raise
        logger.warning({"message": "git host 5xx; one more attempt", "status_code": exc.status})
        return await call()


def finding_error(findings: list[Finding]) -> str:
    """The task error for a scan hit. It names no matched text."""
    first = findings[0]
    text = (
        "the diff holds what looks like a credential "
        f"({first.pattern_name} in {first.path}:{first.line_no})"
    )
    if len(findings) > 1:
        text += f", and {len(findings) - 1} more finding(s)"
    return text


class Manager:
    """The task rules, over one store, one lock manager, and one runtime."""

    def __init__(self, settings: Settings, store: TaskStore, locks: LockManager) -> None:
        self.settings = settings
        self.store = store
        self.locks = locks
        self.runtime: Runtime | None = None
        self.recovered: list[str] = []
        # One event for each task with an open question. answer() sets it.
        self._answer_events: dict[str, asyncio.Event] = {}
        # The tasks whose session log is full. The drop is logged one time.
        self._log_full: set[str] = set()
        # The tasks whose report the manager records now.
        self._finishing: set[str] = set()

    @property
    def config(self) -> DeveloperConfig:
        return self.settings.config

    # --- start -------------------------------------------------------------

    def recover(self) -> list[str]:
        """Clear every lock and fail every task that was active. Returns their ids.

        ``send_recovered_reports`` sends the reports of these tasks later.
        """
        self.locks.clear()
        failed = self.store.fail_active_tasks(RESTART_ERROR)
        for task in failed:
            logger.warning(
                {"message": "task failed by a manager restart", "task_id": task["task_id"]}
            )
        self.recovered = [task["task_id"] for task in failed]
        self.store.clear_ended_worker_tokens()
        return list(self.recovered)

    async def send_recovered_reports(self) -> None:
        """Send the report of each task that ``recover`` failed, once."""
        pending, self.recovered = self.recovered, []
        for task_id in pending:
            full = self.store.get_task_full(task_id)
            if full is not None:
                await notify.send_report(self.settings, full, self._default_notify(full))

    # --- effective values --------------------------------------------------

    def effective_settings(self, person: str) -> dict[str, Any]:
        """The settings of ``person``: the settings row, over developer.yaml, over defaults."""
        row = self.store.get_settings(person)
        entry = self.config.people.get(person)
        git = entry.git if entry else None
        stored_persona = row["default_persona"]
        if stored_persona not in self.config.personas:
            stored_persona = None
        return {
            "person": person,
            "git_name": row["git_name"] or (git.name if git else None),
            "git_email": row["git_email"] or (git.email if git else None),
            "default_persona": stored_persona
            or (entry.default_persona if entry else None)
            or self.config.default_persona,
            "notify": row["notify"] or (entry.notify if entry else None),
        }

    def _default_notify(self, task: dict[str, Any]) -> str | None:
        return self.effective_settings(task["person"])["notify"]

    def allowed_patterns(self, person: str) -> list[str]:
        """The instance repository patterns plus the person's own."""
        entry = self.config.people.get(person)
        return [*self.config.repos, *(entry.repos if entry else [])]

    def resolve_persona(self, person: str | None, requested: str | None) -> str | None:
        """The call argument, else the person's default, else the instance default.

        Returns None when the persona is not known.
        """
        if requested:
            return requested if requested in self.config.personas else None
        if person is None:
            return self.config.default_persona
        return self.effective_settings(person)["default_persona"]

    # --- dispatch ----------------------------------------------------------

    async def develop(
        self,
        person: str,
        repo: str,
        brief: str,
        base_branch: str | None = None,
        branch: str | None = None,
        persona: str | None = None,
        notify_to: str | None = None,
    ) -> dict[str, Any]:
        if not brief or not brief.strip():
            return rejected("invalid_arguments", "brief is required")
        if len(brief) > MAX_TEXT:
            return rejected("invalid_arguments", f"brief is longer than {MAX_TEXT} characters")
        task_id = str(uuid.uuid4())
        try:
            branch_name = check_branch(branch) if branch else default_branch_name(task_id, brief)
            base = check_branch(base_branch) if base_branch else DEFAULT_BASE_BRANCH
        except RepoError as exc:
            return rejected("invalid_arguments", str(exc))
        admitted = self._admit(person, repo)
        if isinstance(admitted, dict):
            return admitted
        return await self._dispatch(
            task_id=task_id,
            person=person,
            task_type="develop",
            repo=admitted[0],
            scope=f"branch:{branch_name}",
            persona=persona,
            notify_to=notify_to,
            brief=brief,
            branch_name=branch_name,
            base_branch=base,
        )

    async def rework(
        self,
        person: str,
        repo: str,
        pr: int | str | None,
        feedback: str,
        persona: str | None = None,
        notify_to: str | None = None,
        branch: str | None = None,
    ) -> dict[str, Any]:
        """Start a task on the source branch of a pull request.

        The platform names the source branch and the base branch. A ``git``
        host has no pull requests, so there ``branch`` names the branch and
        ``pr`` is not used.
        """
        if not feedback or not feedback.strip():
            return rejected("invalid_arguments", "feedback is required")
        if len(feedback) > MAX_TEXT:
            return rejected("invalid_arguments", f"feedback is longer than {MAX_TEXT} characters")
        number: int | None = None
        url: str | None = None
        try:
            if pr is not None and pr != "":
                number, url = parse_pr(pr)
            branch_name = check_branch(branch) if branch else None
        except RepoError as exc:
            return rejected("invalid_arguments", str(exc))
        admitted = self._admit(person, repo)
        if isinstance(admitted, dict):
            return admitted
        normalized, resolved = admitted
        task_id = str(uuid.uuid4())
        if resolved.kind == "git":
            if branch_name is None:
                return rejected(
                    "invalid_arguments",
                    f"the git host {resolved.host} has no pull requests; rework needs branch",
                )
            return await self._dispatch(
                task_id=task_id,
                person=person,
                task_type="rework",
                repo=normalized,
                scope=f"branch:{branch_name}",
                persona=persona,
                notify_to=notify_to,
                instructions=feedback,
                branch_name=branch_name,
            )
        if number is None:
            return rejected("invalid_arguments", "pr is required")
        found = await self._resolve_pr(person, normalized, number)
        if isinstance(found, dict):
            return found
        return await self._dispatch(
            task_id=task_id,
            person=person,
            task_type="rework",
            repo=normalized,
            scope=f"pr:{number}",
            persona=persona,
            notify_to=notify_to,
            instructions=feedback,
            pr_number=number,
            pr_url=url or found.html_url or None,
            branch_name=found.source_branch,
            base_branch=found.base_branch,
        )

    def _admit(self, person: str, raw_repo: str) -> tuple[str, Resolved] | dict[str, Any]:
        """Check the repository before any network call.

        Returns the normalized repository and its platform entry, or a
        rejection: not a repository, not in the list, no platform for the
        host, or no token.
        """
        try:
            repo = normalize_repo(raw_repo)
        except RepoError as exc:
            return rejected("invalid_arguments", str(exc))
        if not repo_allowed(repo, self.allowed_patterns(person)):
            logger.warning({"message": "refused a repository outside the list", "person": person})
            return rejected(
                "repo_not_allowed",
                f"the repository {repo} is not in the list of repositories for {person}",
            )
        try:
            resolved = platforms.resolve(self.config, person, repo)
        except PlatformConfigError as exc:
            logger.warning(
                {"message": "refused a repository with no usable platform", "reason": exc.reason}
            )
            return rejected(exc.reason, str(exc))
        return repo, resolved

    async def _resolve_pr(
        self, person: str, repo: str, number: int
    ) -> platforms.PullRequest | dict[str, Any]:
        """The pull request ``number`` of ``repo``, or a rejection."""
        try:
            platform, _ = platforms.for_task(self.settings, self.config, person, repo)
        except PlatformConfigError as exc:  # pragma: no cover - _admit checked it
            return rejected(exc.reason, str(exc))
        try:
            found = await retry_5xx(lambda: platform.pr(repo, number))
        except PlatformError as exc:
            if exc.status == 404:
                return rejected("pr_not_found", f"the pull request {number} is not in {repo}")
            return rejected(
                "platform_error", f"the git host refused the pull request lookup: {exc}"
            )
        finally:
            await platform.aclose()
        if (found.state or "").lower() in CLOSED_PR_STATES:
            return rejected(
                "pr_not_open",
                f"the pull request {number} is {found.state}; rework needs an open one",
            )
        try:
            check_branch(found.source_branch or "")
            if found.base_branch:
                check_branch(found.base_branch)
        except RepoError:
            return rejected(
                "platform_error", f"the pull request {number} has a branch name the addon refuses"
            )
        return found

    async def _dispatch(
        self,
        *,
        task_id: str,
        person: str,
        task_type: str,
        repo: str,
        scope: str,
        persona: str | None,
        notify_to: str | None,
        **fields: Any,
    ) -> dict[str, Any]:
        """The shared flow: persona, concurrency, lock, row, runtime.

        ``_admit`` has checked the repository.
        """
        chosen = self.resolve_persona(person, persona)
        if chosen is None:
            return rejected(
                "unknown_persona",
                f"the persona {persona!r} is not known; list_personas names the personas",
            )

        refused = self._check_capacity()
        if refused is not None:
            return refused
        refused = self._take_lock(
            repo, scope, task_id, task_type, fields.get("branch_name"), chosen
        )
        if refused is not None:
            return refused

        destination = notify_to or self.effective_settings(person)["notify"]
        self.store.create_task(
            task_id=task_id,
            person=person,
            task_type=task_type,
            repo=repo,
            scope=scope,
            persona=chosen,
            notify=destination,
            worker_token=secrets.token_urlsafe(32),
            **fields,
        )
        logger.info(
            {
                "message": "task dispatched",
                "task_id": task_id,
                "task_type": task_type,
                "person": person,
                "repo": repo,
                "persona": chosen,
            }
        )
        failed = await self._start_worker(task_id)
        if failed is not None:
            return {**failed, "persona": chosen}

        if self.settings.will_notify:
            message = (
                "Task dispatched. Its report arrives as an event from the developer "
                "when it ends. Do not poll. task_status is there for a question now."
            )
        else:
            message = (
                f"Task dispatched. Poll task_status('{task_id}') for progress. "
                "No report event comes, because CHANNELS_URL and "
                "JOSHUA_TOKEN_DEVELOPER are not set."
            )
        return {
            "task_id": task_id,
            "persona": chosen,
            "status": "dispatched",
            "will_notify": self.settings.will_notify,
            "message": message,
        }

    def _check_capacity(self) -> dict[str, Any] | None:
        """A ``concurrency_limit`` rejection when ``max_workers`` workers run, else None."""
        active = self._runtime().count_active()
        limit = self.config.max_workers
        if active >= limit:
            return rejected(
                "concurrency_limit",
                f"{active} developer task(s) already running (limit {limit}). "
                "Retry when one finishes.",
            )
        return None

    def _take_lock(
        self,
        repo: str,
        scope: str,
        task_id: str,
        task_type: str,
        branch: str | None,
        persona: str,
    ) -> dict[str, Any] | None:
        """Take the lock of ``(repo, scope)`` for ``task_id``. A ``locked`` rejection, or None."""
        persona_entry = (
            self.config.personas.get(persona) or self.config.personas[self.config.default_persona]
        )
        ttl = persona_entry.timeout_s + LOCK_MARGIN_S
        try:
            self.locks.acquire(repo, scope, task_id, task_type, branch, ttl)
        except LockedError as exc:
            return rejected(
                "locked",
                str(exc),
                lock_info=exc.lock_info,
                hint="Another task already works on this. Wait for its report, "
                "or check it with task_status.",
            )
        return None

    async def _start_worker(self, task_id: str) -> dict[str, Any] | None:
        """Start the worker of a dispatched task. On a failure, fail the task and free the lock.

        Returns None when the worker started, else the result for the caller.
        """
        full = self.store.get_task_full(task_id)
        assert full is not None
        try:
            await self._runtime().start(full)
        except Exception as exc:
            logger.error(
                {
                    "message": "the runtime could not start the worker",
                    "task_id": task_id,
                    "error_type": type(exc).__name__,
                }
            )
            self.store.update_task(
                task_id,
                status="failed",
                error=f"the worker did not start: {type(exc).__name__}",
                completed_at=now_iso(),
            )
            self._release(full["repo"], full["scope"], task_id)
            return {"task_id": task_id, "status": "failed", "error": "the worker did not start"}
        return None

    def _runtime(self) -> Runtime:
        if self.runtime is None:
            raise RuntimeError("the manager has no runtime")
        return self.runtime

    def _release(self, repo: str, scope: str, task_id: str) -> None:
        try:
            self.locks.release(repo, scope, task_id)
        except LockStolenError:
            logger.warning({"message": "the lock of the task was taken", "task_id": task_id})

    # --- the Reporter side -------------------------------------------------

    def is_active(self, task_id: str, worker_token: str | None = None) -> bool:
        """True when the task is dispatched or running: no report has come.

        With ``worker_token``, also True only while the task still has that
        token, so the supervisor of a worker that a resume replaced does not
        report for the new worker.
        """
        task = self.store.get_task_full(task_id)
        if task is None or task["status"] not in ACTIVE_STATUSES:
            return False
        return worker_token is None or task["worker_token"] == worker_token

    def mark_running(self, task_id: str) -> None:
        """Mark a dispatched task as running."""
        task = self.store.get_task(task_id)
        if task is None or task["status"] != "dispatched":
            return
        self.store.update_task(task_id, status="running", started_at=now_iso())

    async def record_report(self, task_id: str, report: Report) -> dict[str, Any] | None:
        """Record the report of a task, release its lock, and send the report on.

        When the report says the worker pushed, the manager first gates the
        push (``_after_push``). A report for a task that already ended is
        ignored. Returns the task row, or None when the task is not known.
        """
        task = self.store.get_task_full(task_id)
        if task is None:
            return None
        if task["status"] in TERMINAL_STATUSES or task_id in self._finishing:
            logger.warning({"message": "report for an ended task ignored", "task_id": task_id})
            return self.store.get_task(task_id)
        self._finishing.add(task_id)
        try:
            fields: dict[str, Any] = {
                "status": report.status,
                "summary": report.summary,
                "report": report.model_dump(),
                "error": report.error,
                "open_question": report.open_question,
                "commit_hash": report.commit_hash,
                "input_tokens": report.input_tokens,
                "output_tokens": report.output_tokens,
                "estimated_cost": report.estimated_cost,
            }
            if report.pr_url:
                fields["pr_url"] = report.pr_url
            if report.pr_number is not None:
                fields["pr_number"] = report.pr_number
            if report.pushed:
                fields.update(await self._after_push(task, report))
            elif report.branch:
                fields["branch_name"] = report.branch
            fields["completed_at"] = now_iso()
            self.store.update_task(task_id, **fields)
        finally:
            self._finishing.discard(task_id)
        self._release(task["repo"], task["scope"], task_id)
        event = self._answer_events.pop(task_id, None)
        if event is not None:
            event.set()
        self._log_full.discard(task_id)
        logger.info({"message": "task ended", "task_id": task_id, "status": fields["status"]})
        full = self.store.get_task_full(task_id)
        assert full is not None
        await notify.send_report(self.settings, full, self._default_notify(full))
        return self.store.get_task(task_id)

    async def _after_push(self, task: dict[str, Any], report: Report) -> dict[str, Any]:
        """Gate a pushed branch. Returns the task fields to change.

        1. Read the diff of the branch against its base, and scan it. A hit
           deletes the branch and fails the task. A ``git`` host has no diff
           API, so the scan is ``unavailable``.
        2. On a successful ``develop``, find the open pull request of the
           branch, or open one. On a ``rework``, check that the pull request
           is still there.

        A ``PlatformError`` fails the task and keeps the branch. A 5xx gets
        one more attempt.
        """
        task_id = task["task_id"]
        repo = task["repo"]
        head = task["branch_name"]
        if not head or (report.head and report.head != head):
            logger.warning({"message": "worker pushed another branch", "task_id": task_id})
            return {
                "status": "failed",
                "error": f"the worker reported a push to a branch that is not {head}",
            }
        base = task["base_branch"] or DEFAULT_BASE_BRANCH
        try:
            platform, _ = platforms.for_task(self.settings, self.config, task["person"], repo)
        except PlatformConfigError as exc:
            return {"status": "failed", "error": str(exc)}
        out: dict[str, Any] = {}
        step = "the diff"
        try:
            if platform.kind == "git":
                diff = None
            else:
                diff = await retry_5xx(lambda: platform.compare(repo, base, head))
            if diff is None or diff.unavailable:
                out["scan"] = "unavailable"
            else:
                findings = scan_diff(diff)
                if findings:
                    return await self._reject_push(platform, task, findings)
                out["scan"] = "partial" if diff.unscanned else "clean"
            if report.status != "success" or platform.kind == "git":
                return out
            if task["task_type"] == "develop":
                step = "the pull request"

                async def find_or_open() -> platforms.PullRequest:
                    # A second attempt finds the pull request that a first
                    # attempt opened, so a retry never opens two.
                    found = await platform.find_pr(repo, head)
                    if found is None:
                        found = await platform.open_pr(
                            repo, head, base, self._pr_title(task), self._pr_body(task, report)
                        )
                        logger.info({"message": "pull request opened", "task_id": task_id})
                    return found

                found = await retry_5xx(find_or_open)
                out["pr_url"] = found.html_url or found.url
                out["pr_number"] = found.number
            else:
                step = "the pull request"
                number = int(task["pr_number"])
                try:
                    await retry_5xx(lambda: platform.pr(repo, number))
                except PlatformError as exc:
                    if exc.status != 404:
                        raise
                    return {
                        **out,
                        "status": "failed",
                        "error": f"the pull request {number} is gone; the branch {head} stays",
                    }
            return out
        except PlatformError as exc:
            logger.warning(
                {
                    "message": "git host refused a call",
                    "task_id": task_id,
                    "status_code": exc.status,
                }
            )
            return {
                **out,
                "status": "failed",
                "error": (
                    f"the git host refused the call for {step} ({exc}); the branch {head} stays"
                ),
            }
        finally:
            await platform.aclose()

    async def _reject_push(
        self, platform: Platform, task: dict[str, Any], findings: list[Finding]
    ) -> dict[str, Any]:
        """Delete the branch of a push that the scan stopped, and fail the task."""
        logger.warning(
            {
                "message": "scan found a credential; the branch is deleted",
                "task_id": task["task_id"],
                "findings": len(findings),
            }
        )
        error = finding_error(findings)
        try:
            await retry_5xx(lambda: platform.delete_branch(task["repo"], task["branch_name"]))
        except PlatformError as exc:
            error += f". The branch was not deleted ({exc}); delete it by hand"
        return {
            "status": "failed",
            "error": error,
            "scan": "hit",
            "findings": [finding.to_dict() for finding in findings],
            "pr_url": None,
            "pr_number": None,
        }

    def _pr_title(self, task: dict[str, Any]) -> str:
        return brief_title(task["brief"]) or f"Developer task {task['task_id']}"

    def _pr_body(self, task: dict[str, Any], report: Report) -> str:
        person = self.effective_settings(task["person"])
        name = person["git_name"] or task["person"]
        parts = [report.summary.strip() or "No summary."]
        if report.files_changed:
            parts.append("Files changed:\n" + "\n".join(f"- {f}" for f in report.files_changed))
        if report.tests_run:
            parts.append("Tests run:\n" + "\n".join(f"- {t}" for t in report.tests_run))
        parts.append(f"Written by the Joshua developer for {name}. Task {task['task_id']}.")
        return "\n\n".join(parts)

    # --- answer ------------------------------------------------------------

    async def answer(self, person: str, task: dict[str, Any], text: str) -> dict[str, Any]:
        """Give ``task`` the answer to its question.

        A running task with an open question and no answer yet gets the text
        through the long poll of its worker. A task that ended ``blocked`` or
        ``timed_out`` after its worker pushed the branch is resumed: a new
        worker starts on that branch, with the answer in its brief.
        """
        if not text or not text.strip():
            return rejected("invalid_arguments", "text is required")
        if len(text) > MAX_TEXT:
            return rejected("invalid_arguments", f"text is longer than {MAX_TEXT} characters")
        full = self.store.get_task_full(task["task_id"])
        assert full is not None
        if full["status"] in RESUMABLE_STATUSES:
            return await self._resume(person, full, text)
        if full["status"] != "running" or not full["open_question"]:
            return rejected(
                "not_waiting",
                f"task {task['task_id']} has no open question (status {full['status']})",
            )
        if full["answer"]:
            return rejected(
                "not_waiting", f"task {task['task_id']} already has an answer to its question"
            )
        self.store.update_task(task["task_id"], answer=text)
        event = self._answer_events.get(task["task_id"])
        if event is not None:
            event.set()
        logger.info({"message": "answer stored", "task_id": task["task_id"], "person": person})
        return {
            "task_id": task["task_id"],
            "status": "answered",
            "message": "The answer is stored for the worker.",
        }

    async def _resume(self, person: str, full: dict[str, Any], text: str) -> dict[str, Any]:
        """Start a new worker for a task that stopped, where the last worker stopped.

        The task keeps its id, person, repository, branch, persona, and brief.
        The last report moves to ``history``. The new worker gets a new task
        token, and its brief holds the answer and the last summary as a note.
        """
        task_id = full["task_id"]
        history: list[dict[str, Any]] = list(full.get("history") or [])
        previous = full.get("report") or {}
        if not previous.get("pushed") and not any(entry.get("pushed") for entry in history):
            return rejected(
                "not_resumable",
                f"the worker of task {task_id} pushed nothing, so no branch holds its work. "
                "Call develop again with the brief and the answer.",
            )
        refused = self._check_capacity()
        if refused is not None:
            return refused
        refused = self._take_lock(
            full["repo"],
            full["scope"],
            task_id,
            full["task_type"],
            full["branch_name"],
            full["persona"],
        )
        if refused is not None:
            return refused
        history.append({**previous, "completed_at": full["completed_at"]})
        self.store.update_task(
            task_id,
            status="dispatched",
            worker_token=secrets.token_urlsafe(32),
            history=history,
            answer=text,
            report=None,
            summary=None,
            error=None,
            open_question=None,
            scan=None,
            findings=None,
            started_at=None,
            completed_at=None,
        )
        self._log_full.discard(task_id)
        logger.info({"message": "task resumed", "task_id": task_id, "person": person})
        failed = await self._start_worker(task_id)
        if failed is not None:
            return {**failed, "resumed": True}
        return {
            "task_id": task_id,
            "status": "dispatched",
            "resumed": True,
            "message": "A new worker continues the task on its branch, with the answer.",
        }

    # --- the worker side ---------------------------------------------------

    def answer_event(self, task_id: str) -> asyncio.Event:
        """The event that ``answer`` sets for the open question of ``task_id``."""
        event = self._answer_events.get(task_id)
        if event is None:
            event = self._answer_events[task_id] = asyncio.Event()
        return event

    async def ask(self, task_id: str, question: str) -> tuple[bool, str | None]:
        """Open a question for a task and send it to the person's chat.

        The question replaces an earlier one, and clears its answer. The
        question is stored also when nobody gets it. Returns ``(True, None)``
        when channels accepted the event, else ``(False, reason)``: the reason
        is ``no_destination`` when the task and the person have no chat, or
        ``send_failed`` when the event did not go through.
        """
        self.store.update_task(task_id, open_question=question, answer=None)
        old = self._answer_events.get(task_id)
        self._answer_events[task_id] = asyncio.Event()
        if old is not None:
            # A worker that still waits on the old question checks again.
            old.set()
        logger.info({"message": "worker asked a question", "task_id": task_id})
        full = self.store.get_task_full(task_id)
        assert full is not None
        default = self._default_notify(full)
        if not notify.destination_for(self.settings, full, default):
            logger.info({"message": "question not sent: no destination", "task_id": task_id})
            return False, "no_destination"
        if await notify.send_question(self.settings, full, question, default):
            return True, None
        return False, "send_failed"

    def append_log(self, task_id: str, text: str) -> bool:
        """Add worker text to the session log. Returns False when the log is full.

        After the first drop, every later text of the task is dropped too, so
        the log has no gaps.
        """
        if task_id not in self._log_full and self.store.append_session_log(task_id, text):
            return True
        if task_id not in self._log_full:
            self._log_full.add(task_id)
            logger.warning({"message": "session log full; worker text dropped", "task_id": task_id})
        return False
