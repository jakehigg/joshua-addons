"""The manager: the rules that start a task, record its report, and take an answer.

The MCP tools call this class. A call that breaks a rule gets a dict with
``status: "rejected"``, a ``reason``, and a ``message``. The reasons are
``invalid_arguments``, ``repo_not_allowed``, ``unknown_persona``,
``concurrency_limit``, ``locked``, and ``not_waiting``.
"""

from __future__ import annotations

import uuid
from typing import Any

from joshua_developer import notify
from joshua_developer.config import DeveloperConfig, Settings
from joshua_developer.locks import LockedError, LockManager, LockStolenError
from joshua_developer.log import get_logger
from joshua_developer.repos import (
    RepoError,
    check_branch,
    default_branch_name,
    normalize_repo,
    parse_pr,
    repo_allowed,
)
from joshua_developer.runtime import Report, Runtime
from joshua_developer.store import TERMINAL_STATUSES, TaskStore, now_iso

logger = get_logger("joshua_developer.manager")

RESTART_ERROR = "manager restarted"
# The time a lock outlives the persona's timeout before a new task may take it.
LOCK_MARGIN_S = 600
MAX_TEXT = 100_000


def rejected(reason: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"status": "rejected", "reason": reason, "message": message, **extra}


class Manager:
    """The task rules, over one store, one lock manager, and one runtime."""

    def __init__(self, settings: Settings, store: TaskStore, locks: LockManager) -> None:
        self.settings = settings
        self.store = store
        self.locks = locks
        self.runtime: Runtime | None = None
        self.recovered: list[str] = []

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
        return list(self.recovered)

    async def send_recovered_reports(self) -> None:
        """Send the report of each task that ``recover`` failed, once."""
        pending, self.recovered = self.recovered, []
        for task_id in pending:
            full = self.store.get_task_full(task_id)
            if full is not None:
                await notify.send_report(self.settings, full)

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
            branch_name = check_branch(branch) if branch else default_branch_name(task_id)
            base = check_branch(base_branch) if base_branch else None
        except RepoError as exc:
            return rejected("invalid_arguments", str(exc))
        return await self._dispatch(
            task_id=task_id,
            person=person,
            task_type="develop",
            raw_repo=repo,
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
        pr: int | str,
        feedback: str,
        persona: str | None = None,
        notify_to: str | None = None,
    ) -> dict[str, Any]:
        if not feedback or not feedback.strip():
            return rejected("invalid_arguments", "feedback is required")
        if len(feedback) > MAX_TEXT:
            return rejected("invalid_arguments", f"feedback is longer than {MAX_TEXT} characters")
        try:
            number, url = parse_pr(pr)
        except RepoError as exc:
            return rejected("invalid_arguments", str(exc))
        return await self._dispatch(
            task_id=str(uuid.uuid4()),
            person=person,
            task_type="rework",
            raw_repo=repo,
            scope=f"pr:{number}",
            persona=persona,
            notify_to=notify_to,
            instructions=feedback,
            pr_number=number,
            pr_url=url,
        )

    async def _dispatch(
        self,
        *,
        task_id: str,
        person: str,
        task_type: str,
        raw_repo: str,
        scope: str,
        persona: str | None,
        notify_to: str | None,
        **fields: Any,
    ) -> dict[str, Any]:
        """The shared flow: repository, persona, concurrency, lock, row, runtime."""
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
        chosen = self.resolve_persona(person, persona)
        if chosen is None:
            return rejected(
                "unknown_persona",
                f"the persona {persona!r} is not known; list_personas names the personas",
            )

        runtime = self._runtime()
        active = runtime.count_active()
        limit = self.config.max_workers
        if active >= limit:
            return rejected(
                "concurrency_limit",
                f"{active} developer task(s) already running (limit {limit}). "
                "Retry when one finishes.",
            )

        ttl = self.config.personas[chosen].timeout_s + LOCK_MARGIN_S
        try:
            self.locks.acquire(repo, scope, task_id, task_type, fields.get("branch_name"), ttl)
        except LockedError as exc:
            return rejected(
                "locked",
                str(exc),
                lock_info=exc.lock_info,
                hint="Another task already works on this. Wait for its report, "
                "or check it with task_status.",
            )

        destination = notify_to or self.effective_settings(person)["notify"]
        self.store.create_task(
            task_id=task_id,
            person=person,
            task_type=task_type,
            repo=repo,
            scope=scope,
            persona=chosen,
            notify=destination,
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

        full = self.store.get_task_full(task_id)
        assert full is not None
        try:
            await runtime.start(full)
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
            self._release(repo, scope, task_id)
            return {
                "task_id": task_id,
                "persona": chosen,
                "status": "failed",
                "error": "the worker did not start",
            }

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

    def mark_running(self, task_id: str) -> None:
        """Mark a dispatched task as running."""
        task = self.store.get_task(task_id)
        if task is None or task["status"] != "dispatched":
            return
        self.store.update_task(task_id, status="running", started_at=now_iso())

    async def record_report(self, task_id: str, report: Report) -> dict[str, Any] | None:
        """Record the report of a task, release its lock, and send the report on.

        A report for a task that already ended is ignored. Returns the task
        row, or None when the task is not known.
        """
        task = self.store.get_task(task_id)
        if task is None:
            return None
        if task["status"] in TERMINAL_STATUSES:
            logger.warning({"message": "report for an ended task ignored", "task_id": task_id})
            return task
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
            "completed_at": now_iso(),
        }
        if report.branch:
            fields["branch_name"] = report.branch
        if report.pr_url:
            fields["pr_url"] = report.pr_url
        if report.pr_number is not None:
            fields["pr_number"] = report.pr_number
        self.store.update_task(task_id, **fields)
        self._release(task["repo"], task["scope"], task_id)
        logger.info({"message": "task ended", "task_id": task_id, "status": report.status})
        full = self.store.get_task_full(task_id)
        assert full is not None
        await notify.send_report(self.settings, full)
        return self.store.get_task(task_id)

    # --- answer ------------------------------------------------------------

    def answer(self, person: str, task: dict[str, Any], text: str) -> dict[str, Any]:
        """Store the answer to the open question of ``task``.

        A task waits when it is running or blocked, has an open question, and
        has no answer yet.
        """
        if not text or not text.strip():
            return rejected("invalid_arguments", "text is required")
        if len(text) > MAX_TEXT:
            return rejected("invalid_arguments", f"text is longer than {MAX_TEXT} characters")
        full = self.store.get_task_full(task["task_id"])
        assert full is not None
        if full["status"] not in ("running", "blocked") or not full["open_question"]:
            return rejected(
                "not_waiting",
                f"task {task['task_id']} has no open question (status {full['status']})",
            )
        if full["answer"]:
            return rejected(
                "not_waiting", f"task {task['task_id']} already has an answer to its question"
            )
        self.store.update_task(task["task_id"], answer=text)
        logger.info({"message": "answer stored", "task_id": task["task_id"], "person": person})
        return {
            "task_id": task["task_id"],
            "status": "answered",
            "message": "The answer is stored for the worker.",
        }
