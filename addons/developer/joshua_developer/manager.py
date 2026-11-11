"""The manager: the rules that start a task, record its report, and take an answer.

The MCP tools call this class. A call that breaks a rule gets a dict with
``status: "rejected"``, a ``reason``, and a ``message``. The reasons are
``invalid_arguments``, ``repo_not_allowed``, ``repo_not_configured``,
``token_missing``, ``pr_not_found``, ``pr_not_open``, ``platform_error``,
``unknown_persona``, ``concurrency_limit``, ``locked``, ``not_waiting``,
``not_resumable``, and ``branch_is_base``.

A task never works on its base branch, ``main``, or ``master``
(``branch_is_base``). This is a guard, not a boundary: the git token can
push to every branch that the host lets it write.

When a worker reports a push, the manager gates the pull request: it reads
the diff of the worker's commits over the platform API and scans it for
credentials. A hit fails the task, and deletes the branch only when the task
made it. A clean diff gets a pull request. The branch and the pull request
come from the task row and the platform, never from the report.

The manager also serves the worker side: it mints the task token of each
task, takes a question from a worker, and wakes the worker that waits for
the answer. An answer to a task that stopped ``blocked`` or ``timed_out``
after a push starts a new worker for the same task, on the pushed branch.

When a push fails, the worker stores a bundle of its commits on the manager
(``<data dir>/bundles/<task id>.bundle``) before it reports. A task with a
bundle is resumable also when it ``failed``: the new worker restores the
commits. The bundle is deleted when a worker of the task pushes, and after
``BUNDLE_KEEP_DAYS``.

With ``DEVELOPER_WORK_DIR``, each task has its own work directory on storage
that a restart keeps (see ``workspaces``). The directory is deleted only when
the task ends ``success``.

The task clock pauses while a question waits. ``asked_at`` holds the start
of the open wait, and ``paused_s`` the seconds of the waits that ended. The
supervisor of the runtime adds both to the deadline of the worker.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from joshua_developer import notify, platforms
from joshua_developer.config import DeveloperConfig, Settings
from joshua_developer.locks import LockedError, LockManager
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
from joshua_developer.runtime import Report, Runtime, TaskClock
from joshua_developer.scan import Finding, scan_diff
from joshua_developer.store import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    TaskStore,
    now_iso,
    parse_time,
    utcnow,
)
from joshua_developer.workspaces import Workspaces

logger = get_logger("joshua_developer.manager")

# The error of an active task whose worker is not there after a manager restart.
WORKER_GONE_ERROR = "the worker is gone: the manager restarted and found no worker for the task"
MAX_TEXT = 100_000
MAX_QUESTION = 10_000
DEFAULT_BASE_BRANCH = "main"
# A task never works on one of these, or on its own base branch.
PROTECTED_BRANCHES = frozenset({"main", "master"})
# The caps on the stored report fields. A longer value is cut, with
# TRUNCATED at the end. A report is never refused for its size alone.
TRUNCATED = "\n[truncated by the developer manager]"
MAX_REPORT_LOG = 1024 * 1024
MAX_REPORT_SUMMARY = 20_000
MAX_REPORT_FILES = 500
MAX_REPORT_FILE = 512
MAX_REPORT_TESTS = 50
MAX_REPORT_TEST = 2000
MAX_REPORT_QUESTION = 10_000
MAX_REPORT_ERROR = 20_000
# A git commit name: 40 (SHA-1) or 64 (SHA-256) hex characters.
_COMMIT_CHARS = frozenset("0123456789abcdef")
# The states of a pull request that a rework cannot change.
CLOSED_PR_STATES = frozenset({"closed", "merged", "locked"})
# The states from which an answer resumes a task.
RESUMABLE_STATUSES = ("blocked", "timed_out")
# The most reference repositories of one task (refs).
MAX_REFS = 5
# The bundles of commits that a worker could not push.
BUNDLE_DIR = "bundles"
MAX_BUNDLE = 100 * 1024 * 1024
BUNDLE_KEEP_DAYS = 14
# The first line of a git bundle file, version 2 or 3.
BUNDLE_SIGNATURES = (b"# v2 git bundle\n", b"# v3 git bundle\n")
# The time between two runs of the sweeper of the work directories.
SWEEP_INTERVAL_S = 3600


def rejected(reason: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"status": "rejected", "reason": reason, "message": message, **extra}


def branch_is_base(branch: str, base: str | None) -> dict[str, Any] | None:
    """A ``branch_is_base`` rejection when ``branch`` is ``base``, ``main``, or ``master``."""
    if branch == base or branch in PROTECTED_BRANCHES:
        return rejected(
            "branch_is_base",
            f"the branch {branch} is a base or default branch; a task works on its own branch",
        )
    return None


def cut(text: str | None, limit: int) -> str | None:
    """``text``, cut to ``limit`` characters with the ``TRUNCATED`` marker."""
    if text is None or len(text) <= limit:
        return text
    return text[: max(0, limit - len(TRUNCATED))] + TRUNCATED


def cut_list(items: list[str], count: int, limit: int) -> list[str]:
    """At most ``count`` items, each cut to ``limit``. A cut list ends with ``TRUNCATED``."""
    out = [cut(item, limit) or "" for item in items[:count]]
    if len(items) > count:
        out.append(TRUNCATED.strip())
    return out


def capped(report: Report) -> Report:
    """``report`` with each stored text field cut to its cap."""
    return report.model_copy(
        update={
            "summary": cut(report.summary, MAX_REPORT_SUMMARY),
            "log": cut(report.log, MAX_REPORT_LOG),
            "files_changed": cut_list(report.files_changed, MAX_REPORT_FILES, MAX_REPORT_FILE),
            "tests_run": cut_list(report.tests_run, MAX_REPORT_TESTS, MAX_REPORT_TEST),
            "open_question": cut(report.open_question, MAX_REPORT_QUESTION),
            "error": cut(report.error, MAX_REPORT_ERROR),
        }
    )


def is_commit(value: str | None) -> bool:
    """True when ``value`` is a full commit name in lowercase hex."""
    return bool(value) and len(value or "") in (40, 64) and set(value or "") <= _COMMIT_CHARS


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

    def __init__(
        self,
        settings: Settings,
        store: TaskStore,
        locks: LockManager,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.settings = settings
        self.store = store
        self.locks = locks
        self.clock = clock
        self.runtime: Runtime | None = None
        self.recovered: list[str] = []
        # One event for each task with an open question. answer() sets it.
        self._answer_events: dict[str, asyncio.Event] = {}
        # The tasks whose session log is full. The drop is logged one time.
        self._log_full: set[str] = set()
        # The tasks whose report the manager records now.
        self._finishing: set[str] = set()
        # Held around the capacity check, the lock, and the row write of a
        # dispatch or a resume, so two calls cannot both take the last slot.
        self._admission = threading.Lock()
        self.workspaces = Workspaces(settings.work_dir, store, settings.config.work.keep_failed)

    @property
    def config(self) -> DeveloperConfig:
        return self.settings.config

    # --- start -------------------------------------------------------------

    def recover(self) -> list[str]:
        """Take the locks of the tasks that are still active. Returns their ids.

        A manager restart does not end a task: its worker can still run.
        ``reattach_workers`` then finds the worker of each active task, and
        fails only a task whose worker is gone.
        """
        self.locks.clear()
        active = self.store.list_active_tasks()
        for task in active:
            full = self.store.get_task_full(task["task_id"]) or task
            try:
                self.locks.acquire(
                    full["repo"],
                    full["scope"],
                    full["task_id"],
                    full["task_type"],
                    full.get("branch_name"),
                )
            except LockedError:
                logger.warning(
                    {"message": "two active tasks hold one lock", "task_id": full["task_id"]}
                )
        self.store.clear_ended_worker_tokens()
        self.prune_bundles()
        self.sweep_workspaces()
        return [task["task_id"] for task in active]

    async def reattach_workers(self) -> list[str]:
        """Watch the worker of each active task again. Returns the ids of the failed tasks.

        A task whose worker is there, running or ended, keeps its status, and
        a supervisor watches its worker again: a worker that ended while the
        manager was away gives its outcome. A task with no worker fails with
        ``WORKER_GONE_ERROR``, keeps its work directory, and its report goes
        out with ``send_recovered_reports``. Then the runtime removes the
        workers that no active task owns.
        """
        runtime = self._runtime()
        failed: list[str] = []
        for task in self.store.list_active_tasks():
            task_id = task["task_id"]
            full = self.store.get_task_full(task_id)
            assert full is not None
            try:
                found = await runtime.reattach(full)
            except Exception as exc:
                logger.error(
                    {
                        "message": "the runtime could not look for the worker",
                        "task_id": task_id,
                        "error_type": type(exc).__name__,
                    }
                )
                found = False
            if found:
                continue
            self.store.update_task(
                task_id,
                status="failed",
                error=WORKER_GONE_ERROR,
                completed_at=now_iso(),
                asked_at=None,
            )
            self.locks.release(full["repo"], full["scope"], task_id)
            logger.warning({"message": "task failed: its worker is gone", "task_id": task_id})
            failed.append(task_id)
        self.recovered.extend(failed)
        try:
            await asyncio.to_thread(runtime.remove_orphans)
        except Exception as exc:
            logger.warning(
                {"message": "the old workers were not removed", "error_type": type(exc).__name__}
            )
        return failed

    # --- work directories --------------------------------------------------

    def sweep_workspaces(self) -> int:
        """Run the sweeper of the work directories once. Returns the count it deleted."""
        try:
            return self.workspaces.sweep()
        except OSError as exc:
            logger.warning(
                {"message": "the workspace sweep failed", "error_type": type(exc).__name__}
            )
            return 0

    async def sweep_forever(self, interval_s: float = SWEEP_INTERVAL_S) -> None:
        """Run the sweeper every ``interval_s`` seconds, until cancelled."""
        while True:
            await asyncio.sleep(interval_s)
            await asyncio.to_thread(self.sweep_workspaces)

    # --- bundles -----------------------------------------------------------

    @property
    def bundle_dir(self) -> Path:
        return self.settings.data_dir / BUNDLE_DIR

    def bundle_path(self, task_id: str) -> Path:
        """The bundle file of a task. The task id comes from the store, never from a request."""
        return self.bundle_dir / f"{task_id}.bundle"

    def has_bundle(self, task_id: str) -> bool:
        return self.bundle_path(task_id).is_file()

    def drop_bundle(self, task_id: str) -> None:
        """Delete the bundle of a task, when it has one."""
        with contextlib.suppress(FileNotFoundError):
            self.bundle_path(task_id).unlink()
            logger.info({"message": "bundle deleted", "task_id": task_id})

    def prune_bundles(self, now: float | None = None) -> int:
        """Delete each bundle older than ``BUNDLE_KEEP_DAYS``. Returns the count."""
        if not self.bundle_dir.is_dir():
            return 0
        limit = (now if now is not None else time.time()) - BUNDLE_KEEP_DAYS * 86400
        count = 0
        for path in self.bundle_dir.iterdir():
            with contextlib.suppress(OSError):
                if path.stat().st_mtime < limit:
                    path.unlink()
                    count += 1
        if count:
            logger.info({"message": "old bundles deleted", "count": count})
        return count

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
        refs: list[str] | None = None,
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
        refused = branch_is_base(branch_name, base)
        if refused is not None:
            return refused
        admitted = self._admit(person, repo)
        if isinstance(admitted, dict):
            return admitted
        checked = self._admit_refs(person, admitted[0], refs)
        if isinstance(checked, dict):
            return checked
        return await self._dispatch(
            task_id=task_id,
            person=person,
            task_type="develop",
            refs=checked or None,
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
        refs: list[str] | None = None,
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
        checked = self._admit_refs(person, normalized, refs)
        if isinstance(checked, dict):
            return checked
        task_id = str(uuid.uuid4())
        if resolved.kind == "git":
            if branch_name is None:
                return rejected(
                    "invalid_arguments",
                    f"the git host {resolved.host} has no pull requests; rework needs branch",
                )
            refused = branch_is_base(branch_name, None)
            if refused is not None:
                return refused
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
                refs=checked or None,
            )
        if number is None:
            return rejected("invalid_arguments", "pr is required")
        found = await self._resolve_pr(person, normalized, number)
        if isinstance(found, dict):
            return found
        refused = branch_is_base(found.source_branch or "", found.base_branch)
        if refused is not None:
            return refused
        return await self._dispatch(
            task_id=task_id,
            person=person,
            task_type="rework",
            repo=normalized,
            # The branch, as for develop: a develop and a rework on the same
            # branch never run together.
            scope=f"branch:{found.source_branch}",
            persona=persona,
            notify_to=notify_to,
            instructions=feedback,
            pr_number=number,
            pr_url=url or found.html_url or None,
            branch_name=found.source_branch,
            base_branch=found.base_branch,
            refs=checked or None,
        )

    def _admit_refs(
        self, person: str, repo: str, raw_refs: list[str] | None
    ) -> list[str] | dict[str, Any]:
        """Check the reference repositories of a task. Returns them normalized, or a rejection.

        A reference repository is cloned read-only next to the checkout, so the
        worker can read it. Each one passes the same checks as the task
        repository. The task repository itself and a repeated entry are refused.
        """
        if not raw_refs:
            return []
        if len(raw_refs) > MAX_REFS:
            return rejected("invalid_arguments", f"refs holds at most {MAX_REFS} repositories")
        refs: list[str] = []
        for raw in raw_refs:
            admitted = self._admit(person, raw)
            if isinstance(admitted, dict):
                return {**admitted, "message": f"refs: {admitted['message']}"}
            ref = admitted[0]
            if ref == repo:
                return rejected(
                    "invalid_arguments", f"refs: {ref} is the task repository; it is cloned already"
                )
            if ref in refs:
                return rejected("invalid_arguments", f"refs: {ref} is named twice")
            refs.append(ref)
        return refs

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

        destination = notify_to or self.effective_settings(person)["notify"]
        with self._admission:
            refused = self._check_capacity()
            if refused is not None:
                return refused
            refused = self._take_lock(repo, scope, task_id, task_type, fields.get("branch_name"))
            if refused is not None:
                return refused
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
        """A ``concurrency_limit`` rejection when ``max_workers`` tasks are active, else None.

        It counts the task rows in ``dispatched`` or ``running``, so a task
        whose worker is still starting takes its slot. Call it with
        ``_admission`` held, together with the row write.
        """
        self._runtime()
        active = self.store.count_active_tasks()
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
    ) -> dict[str, Any] | None:
        """Take the lock of ``(repo, scope)`` for ``task_id``. A ``locked`` rejection, or None."""
        try:
            self.locks.acquire(repo, scope, task_id, task_type, branch)
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
            await asyncio.to_thread(self.workspaces.prepare, full)
        except OSError as exc:
            logger.error(
                {
                    "message": "the work directory was not made",
                    "task_id": task_id,
                    "error_type": type(exc).__name__,
                }
            )
            self.store.update_task(
                task_id,
                status="failed",
                error=f"the work directory was not made: {type(exc).__name__}; "
                "check that the manager can write DEVELOPER_WORK_DIR",
                completed_at=now_iso(),
            )
            self.locks.release(full["repo"], full["scope"], task_id)
            return {"task_id": task_id, "status": "failed", "error": "the worker did not start"}
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
            self.locks.release(full["repo"], full["scope"], task_id)
            return {"task_id": task_id, "status": "failed", "error": "the worker did not start"}
        return None

    def _runtime(self) -> Runtime:
        if self.runtime is None:
            raise RuntimeError("the manager has no runtime")
        return self.runtime

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
        """Mark a dispatched task as running, and start its clock (``started_at``).

        The worker API calls it when the worker reads its brief, so the time
        to pull the image and start the container is not on the task clock.
        A later call changes nothing.
        """
        task = self.store.get_task(task_id)
        if task is None or task["status"] != "dispatched":
            return
        self.store.update_task(task_id, status="running", started_at=now_iso())

    def task_clock(self, task_id: str) -> TaskClock | None:
        """The clock fields of a task for its supervisor, or None when the task is not known."""
        task = self.store.get_task_full(task_id)
        if task is None:
            return None
        return TaskClock(
            started_at=parse_time(task["started_at"]),
            paused_s=int(task["paused_s"] or 0),
            asked_at=parse_time(task["asked_at"]),
        )

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
        report = capped(report)
        try:
            self.store.end_wait(task_id, self.clock())
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
            if report.pushed:
                fields.update(await self._after_push(task, report))
                # The commits reached the git host, so a saved bundle has no use.
                self.drop_bundle(task_id)
            fields["completed_at"] = now_iso()
            self.store.update_task(task_id, **fields)
        finally:
            self._finishing.discard(task_id)
        if fields["status"] == "success":
            # Only a task that ended success loses its work directory.
            await asyncio.to_thread(self.workspaces.drop, task_id)
        self.locks.release(task["repo"], task["scope"], task_id)
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

        1. Read the diff from ``clone_head`` (the branch before the session)
           to the pushed branch, so the scan reads only the worker's commits,
           and scan it. Without a ``clone_head``, the diff starts at the base
           branch. A hit fails the task (``_reject_push``). A ``git`` host has
           no diff API, so the scan is ``unavailable``.
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
        # The worker names clone_head. A wrong one narrows the scan, but a
        # worker can push to the branch with its git token in any case: the
        # scan is a guard against a mistake, not against a hostile worker.
        since = report.clone_head if is_commit(report.clone_head) else base
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
                diff = await retry_5xx(lambda: platform.compare(repo, since, head))
            if diff is None or diff.unavailable:
                out["scan"] = "unavailable"
            else:
                findings = scan_diff(diff)
                if findings:
                    return await self._reject_push(platform, task, report, findings)
                out["scan"] = "partial" if diff.unscanned or diff.truncated else "clean"
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
        self, platform: Platform, task: dict[str, Any], report: Report, findings: list[Finding]
    ) -> dict[str, Any]:
        """Fail the task of a push that the scan stopped.

        The manager deletes the branch only when this task made it: a
        ``develop`` whose worker, or an earlier worker of the same task,
        reports ``created_branch``. A branch that was there before the task
        stays, and the error says so. A ``rework`` never deletes.
        """
        history = task.get("history") or []
        created = task["task_type"] == "develop" and (
            report.created_branch or any(entry.get("created_branch") for entry in history)
        )
        logger.warning(
            {
                "message": "scan found a credential",
                "task_id": task["task_id"],
                "findings": len(findings),
                "branch_deleted": created,
            }
        )
        error = finding_error(findings)
        if created:
            try:
                await retry_5xx(lambda: platform.delete_branch(task["repo"], task["branch_name"]))
            except PlatformError as exc:
                error += f". The branch was not deleted ({exc}); delete it by hand"
        else:
            error += (
                f". The branch {task['branch_name']} was kept, because it existed before "
                "the task; remove the commit by hand"
            )
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
        through the long poll of its worker, and its clock starts again. A
        task that ended ``blocked`` or
        ``timed_out`` after its worker pushed the branch is resumed: a new
        worker starts on that branch, with the answer in its brief. A task
        that ended with a saved bundle, or with a checkout in its kept work
        directory (``failed`` included), is resumed from it.
        """
        if not text or not text.strip():
            return rejected("invalid_arguments", "text is required")
        if len(text) > MAX_TEXT:
            return rejected("invalid_arguments", f"text is longer than {MAX_TEXT} characters")
        full = self.store.get_task_full(task["task_id"])
        assert full is not None
        if full["status"] in RESUMABLE_STATUSES or (
            full["status"] == "failed" and self.has_saved_work(full["task_id"])
        ):
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
        if not full["asked_at"]:
            # The worker stopped waiting, or the question was never sent. An
            # answer stored now would reach no one.
            return rejected(
                "not_waiting",
                f"the worker of task {task['task_id']} no longer waits for an answer. "
                "The task will end blocked or success. Then call answer again to resume it "
                "with this answer.",
            )
        self.store.end_wait(task["task_id"], self.clock())
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
        pushed = previous.get("pushed") or any(entry.get("pushed") for entry in history)
        if not pushed and not self.has_saved_work(task_id):
            return rejected(
                "not_resumable",
                f"the worker of task {task_id} pushed nothing and saved no commits, so "
                "nothing holds its work. Call develop again with the brief and the answer.",
            )
        history.append({**previous, "completed_at": full["completed_at"]})
        with self._admission:
            refused = self._check_capacity()
            if refused is not None:
                return refused
            refused = self._take_lock(
                full["repo"], full["scope"], task_id, full["task_type"], full["branch_name"]
            )
            if refused is not None:
                return refused
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
                asked_at=None,
                paused_s=0,
                last_wait_s=None,
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

    def has_saved_work(self, task_id: str) -> bool:
        """True when a bundle, or a kept checkout in the work directory, holds the task's work."""
        return self.has_bundle(task_id) or self.workspaces.has_checkout(task_id)

    def push_only(self, task: dict[str, Any]) -> bool:
        """True when a resumed worker only pushes the saved commits, with no session.

        The last worker finished its session, and only its push failed.
        """
        history = task.get("history") or []
        previous = history[-1] if history else {}
        return (
            self.has_bundle(task["task_id"])
            and bool(previous.get("bundle_saved"))
            and previous.get("unpushed_status") == "success"
        )

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
        question is stored also when nobody gets it. A question that was sent
        pauses the task clock until the answer comes or the worker stops
        waiting. Returns ``(True, None)``
        when channels accepted the event, else ``(False, reason)``: the reason
        is ``no_destination`` when the task and the person have no chat, or
        ``send_failed`` when the event did not go through.
        """
        self.store.end_wait(task_id, self.clock())
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
        # The wait opens before the send, so an answer that comes back at
        # once finds it open.
        self.store.update_task(task_id, asked_at=self.clock().isoformat())
        if await notify.send_question(self.settings, full, question, default):
            return True, None
        self.store.update_task(task_id, asked_at=None)
        return False, "send_failed"

    def stop_wait(self, task_id: str) -> int:
        """The worker stops waiting for an answer. The clock starts again.

        The question stays open. Returns the total paused seconds of the task.
        """
        waited = self.store.end_wait(task_id, self.clock())
        if waited is not None:
            logger.info(
                {"message": "worker stopped waiting", "task_id": task_id, "waited_s": waited}
            )
        full = self.store.get_task_full(task_id)
        return int(full["paused_s"] or 0) if full else 0

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
