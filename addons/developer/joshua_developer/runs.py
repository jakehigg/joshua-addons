"""Development auto-run: one task of a tasks project after the other.

``run_project`` starts a run. A run owns one branch and one pull request of
one repository, and it works through the open tasks of one tasks project in
roadmap order. When the developer task of a run ends, ``Runs.step`` takes the
next step: it marks the tasks task done, checks the limits, reads the next
task, and dispatches it as a ``rework`` of the run's pull request. It never
merges.

A step costs no model turn. The person's chat gets a verbatim line for each
finished task. A model turn comes only when a person must act: a failure that
a retry did not fix, a question, the end of a phase (a task group), a limits
ceiling, a blocked project, or the end of the project.

The tasks addon is called at ``TASKS_URL`` (its ``/api``, which has no
bearer). Core is called at ``CORE_URL`` for ``GET /v1/limits``, which also has
no bearer. A step never raises into ``record_report``.
"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING, Any

import httpx

from joshua_developer import notify
from joshua_developer.log import get_logger
from joshua_developer.repos import RepoError, check_branch

if TYPE_CHECKING:
    from joshua_developer.manager import Manager

logger = get_logger("joshua_developer.runs")

REQUEST_TIMEOUT_S = 10.0
LIMITS_PATH = "/v1/limits"
WINDOWS = (("five_hour", "5-hour"), ("seven_day", "weekly"))
# The lines of a task's notes that the run adds. A brief never holds them.
_RUN_NOTE = re.compile(r"^(dispatched\b|done in\b)", re.IGNORECASE)
BRIEF_TAIL = "Commit and push your work."
MAX_TEXT = 1500


class TasksError(Exception):
    """A call to the tasks addon failed. ``status`` is the HTTP status, or None."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S)


def rejected(reason: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"status": "rejected", "reason": reason, "message": message, **extra}


def brief_of(task: dict[str, Any]) -> str:
    """The brief of a developer task: the title, the notes, and the push line.

    The lines that the run wrote into the notes are left out.
    """
    notes = "\n".join(
        line for line in (task.get("notes") or "").splitlines() if not _RUN_NOTE.match(line.strip())
    ).strip()
    parts = [str(task["title"]).strip()]
    if notes:
        parts.append(notes)
    parts.append(BRIEF_TAIL)
    return "\n\n".join(parts)


def append_note(notes: str | None, line: str) -> str:
    return f"{notes.rstrip()}\n{line}" if notes and notes.strip() else line


def label(task: dict[str, Any]) -> str:
    return f"#{task['number']} {task['title']}"


class TasksClient:
    """The calls of a run to the tasks ``/api``."""

    def __init__(self, base_url: str) -> None:
        base = base_url.rstrip("/")
        self.base = base if base.endswith("/api") else base + "/api"

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            async with make_client() as client:
                response = await client.request(method, self.base + path, **kwargs)
        except httpx.HTTPError as exc:
            raise TasksError(f"the tasks addon is not reachable ({type(exc).__name__})") from exc
        if response.status_code >= 300:
            raise TasksError(
                f"the tasks addon answered {response.status_code} to {method} {path}",
                response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise TasksError(f"the tasks addon sent no JSON for {method} {path}") from exc

    async def next_step(self, project: str) -> dict[str, Any]:
        """The project, with ``tasks`` holding its first ready task, if any."""
        found = await self._call("GET", "/next-steps", params={"project": project, "limit": 1})
        if not isinstance(found, list) or not found:
            raise TasksError(f"the tasks addon has no project {project!r}", 404)
        return found[0]

    async def by_number(self, number: int) -> dict[str, Any]:
        return await self._call("GET", f"/tasks/by-number/{number}")

    async def patch(self, task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._call("PATCH", f"/tasks/{task_id}", json=body)

    async def group_name(self, project_id: str, group_id: str | None) -> str | None:
        """The name of a task group, or None when it is not known."""
        if not group_id:
            return None
        try:
            groups = await self._call("GET", f"/projects/{project_id}/groups")
        except TasksError:
            return None
        for group in groups if isinstance(groups, list) else []:
            if group.get("id") == group_id:
                return str(group.get("name") or "") or None
        return None


class Runs:
    """The runs of one manager."""

    def __init__(self, manager: Manager) -> None:
        self.manager = manager

    @property
    def store(self):  # noqa: ANN201 - the store type is in the manager
        return self.manager.store

    def _tasks(self) -> TasksClient | None:
        url = self.manager.settings.tasks_url
        return TasksClient(url) if url else None

    # --- the tools ---------------------------------------------------------

    async def start(
        self,
        person: str,
        repo: str,
        project: str,
        branch: str | None = None,
        persona: str | None = None,
        notify_to: str | None = None,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Start a run, and dispatch its first task."""
        manager = self.manager
        client = self._tasks()
        if client is None:
            return rejected(
                "tasks_not_configured", "TASKS_URL is not set; a run needs the tasks addon"
            )
        if not project or not project.strip():
            return rejected("invalid_arguments", "project is required")
        admitted = manager._admit(person, repo)
        if isinstance(admitted, dict):
            return admitted
        normalized = admitted[0]
        if persona and manager.resolve_persona(person, persona) is None:
            return rejected(
                "unknown_persona",
                f"the persona {persona!r} is not known; list_personas names the personas",
            )
        try:
            entry = await client.next_step(project.strip())
        except TasksError as exc:
            reason = "project_not_found" if exc.status == 404 else "tasks_unreachable"
            return rejected(reason, str(exc))
        project_id = str(entry["id"])
        slug = str(entry.get("slug") or project_id)
        try:
            branch_name = check_branch(branch) if branch else check_branch(f"feat-{slug}")
        except RepoError as exc:
            return rejected("invalid_arguments", str(exc))
        existing = self.store.find_open_run(normalized, project_id)
        if existing is not None:
            return rejected(
                "run_exists",
                f"run {existing['run_id']} ({existing['status']}) already works on this "
                "project in this repository",
                run_id=existing["run_id"],
            )
        ready = entry.get("tasks") or []
        if not ready:
            reason = (
                "all the open tasks of the project are blocked"
                if entry.get("open_count")
                else "the project has no open task"
            )
            return rejected("no_tasks", reason)
        run_id = str(uuid.uuid4())
        self.store.create_run(
            run_id=run_id,
            person=person,
            repo=normalized,
            project=project_id,
            project_name=entry.get("name"),
            branch=branch_name,
            workspace=workspace,
            persona=persona,
            notify=notify_to,
        )
        run = self.store.get_run(run_id)
        assert run is not None
        result = await self._dispatch(run, ready[0], client)
        if result.get("status") == "rejected":
            self.store.update_run(run_id, status="stopped")
            return result
        return {
            "run_id": run_id,
            "project": entry.get("name") or project_id,
            "repo": normalized,
            "branch": branch_name,
            "status": "active",
            "task": {"number": ready[0]["number"], "title": ready[0]["title"]},
            "task_id": result.get("task_id"),
            "message": (
                "Run started. Each finished task starts the next one with no model turn. "
                "You get a message at the end of a phase, at a limits ceiling, on a "
                "question, on a failure the retry did not fix, and at the end of the project."
            ),
        }

    def find(self, person: str | None, run_id: str | None, project: str | None) -> dict[str, Any]:
        """The run for a ``run_id`` or a ``project``, or a rejection."""
        if run_id:
            run = self.store.get_run(run_id)
            if run is None or (person is not None and run["person"] != person):
                return rejected("run_not_found", f"there is no run {run_id}")
            return run
        if not project:
            return rejected("invalid_arguments", "give run_id or project")
        wanted = project.strip().casefold()
        runs = [
            r
            for r in self.store.list_runs(person, limit=100)
            if wanted in (r["project"].casefold(), (r["project_name"] or "").casefold())
            or r["branch"].casefold() == f"feat-{wanted}"
        ]
        open_runs = [r for r in runs if r["status"] in ("active", "paused")]
        pool = open_runs or runs[:1]
        if not pool:
            return rejected("run_not_found", f"there is no run for the project {project!r}")
        if len(pool) > 1:
            return rejected(
                "run_ambiguous",
                "more than one run works on this project; give run_id",
                run_ids=[r["run_id"] for r in pool],
            )
        return pool[0]

    async def pause(self, run: dict[str, Any]) -> dict[str, Any]:
        if run["status"] == "paused":
            return self.describe(run, "The run is paused already.")
        if run["status"] != "active":
            return rejected("run_not_pausable", f"the run is {run['status']}")
        self.store.update_run(run["run_id"], status="paused")
        return self.describe(
            self.store.get_run(run["run_id"]),
            "The run is paused. The task that runs now goes on to its end; no next task starts.",
        )

    async def stop(self, run: dict[str, Any]) -> dict[str, Any]:
        if run["status"] in ("stopped", "done"):
            return self.describe(run, f"The run is {run['status']} already.")
        self.store.update_run(run["run_id"], status="stopped")
        return self.describe(
            self.store.get_run(run["run_id"]),
            "The run is stopped. The task that runs now goes on to its end; no next task starts.",
        )

    async def resume(self, run: dict[str, Any]) -> dict[str, Any]:
        """Make a paused run active, and dispatch its next task.

        An ``active`` run with no developer task (a manager restart ended its
        task) is also dispatched. A run whose task still runs only becomes
        active: the step of that task goes on.
        """
        if run["status"] in ("stopped", "done"):
            return rejected("run_not_resumable", f"the run is {run['status']}")
        client = self._tasks()
        if client is None:
            return rejected(
                "tasks_not_configured", "TASKS_URL is not set; a run needs the tasks addon"
            )
        run_id = run["run_id"]
        if self.store.run_has_active_task(run_id):
            self.store.update_run(run_id, status="active", retried=False)
            return self.describe(self.store.get_run(run_id), "The run is active. Its task goes on.")
        try:
            entry = await client.next_step(run["project"])
        except TasksError as exc:
            return rejected("tasks_unreachable", str(exc))
        ready = entry.get("tasks") or []
        if not ready:
            done = not entry.get("open_count")
            self.store.update_run(run_id, status="done" if done else "paused")
            return self.describe(
                self.store.get_run(run_id),
                "The project has no open task."
                if done
                else "All the open tasks of the project are blocked.",
            )
        self.store.update_run(run_id, status="active", retried=False)
        result = await self._dispatch(self.store.get_run(run_id) or run, ready[0], client)
        if result.get("status") == "rejected":
            self.store.update_run(run_id, status="paused")
            return result
        return self.describe(
            self.store.get_run(run_id), f"The run is active. Task {label(ready[0])} started."
        )

    def describe(self, run: dict[str, Any] | None, message: str | None = None) -> dict[str, Any]:
        """The run as a tool result, with its current developer task."""
        assert run is not None
        task = self.store.get_task(run["current_task_id"]) if run["current_task_id"] else None
        result: dict[str, Any] = {
            "run_id": run["run_id"],
            "project": run["project_name"] or run["project"],
            "repo": run["repo"],
            "branch": run["branch"],
            "status": run["status"],
            "pr_url": run["pr_url"],
            "pr_number": run["pr_number"],
            "current_task_number": run["current_task_number"],
            "current_task": {
                "task_id": task["task_id"],
                "status": task["status"],
                "error": task["error"],
                "open_question": task["open_question"],
            }
            if task
            else None,
            "retried": run["retried"],
            "created_at": run["created_at"],
            "updated_at": run["updated_at"],
        }
        if message:
            result["message"] = message
        return result

    # --- the dispatch ------------------------------------------------------

    async def _dispatch(
        self, run: dict[str, Any], task: dict[str, Any], client: TasksClient, note: str = ""
    ) -> dict[str, Any]:
        """Dispatch a tasks task: a ``rework`` of the run's pull request, else a ``develop``.

        On success it stores the task on the run, and adds a line to the notes.
        """
        manager = self.manager
        link = {
            "run_id": run["run_id"],
            "tasks_task_id": str(task["id"]),
            "tasks_task_number": int(task["number"]),
        }
        brief = brief_of(task)
        if run["pr_number"]:
            result = await manager.rework(
                run["person"],
                run["repo"],
                run["pr_number"],
                brief,
                persona=run["persona"],
                notify_to=run["notify"],
                branch=run["branch"],
                workspace=run["workspace"],
                **link,
            )
        else:
            result = await manager.develop(
                run["person"],
                run["repo"],
                brief,
                branch=run["branch"],
                persona=run["persona"],
                notify_to=run["notify"],
                workspace=run["workspace"],
                **link,
            )
        if result.get("status") == "rejected":
            return result
        line = f"dispatched{note} as developer task {result['task_id']} on {run['branch']}"
        try:
            # Read the notes again: they may have changed since the task was read.
            fresh = await client.by_number(link["tasks_task_number"])
            await client.patch(
                link["tasks_task_id"], {"notes": append_note(fresh.get("notes"), line)}
            )
        except TasksError as exc:
            logger.warning({"message": "run: the dispatch note was not written", "error": str(exc)})
        return result

    # --- the step ----------------------------------------------------------

    async def step(self, task: dict[str, Any]) -> bool:
        """The step after the report of a task of a run. Never raises.

        Returns True when the normal report event must go out, and False when
        the step sent its own event.
        """
        run = self.store.get_run(task["run_id"]) if task.get("run_id") else None
        if run is None:
            return True
        try:
            return await self._step(run, task)
        except TasksError as exc:
            logger.warning({"message": "run: the tasks addon failed", "error": str(exc)})
            await self._pause(
                run,
                f"Run on {run['repo']} paused: {exc}. "
                f"Pull request: {task.get('pr_url') or 'none'}.",
            )
            return False
        except Exception as exc:
            logger.error(
                {"message": "run: the step failed", "error_type": type(exc).__name__},
                exc_info=True,
            )
            self.store.update_run(run["run_id"], status="paused")
            return True

    async def _step(self, run: dict[str, Any], task: dict[str, Any]) -> bool:
        if not run["pr_number"] and task.get("pr_number"):
            self.store.update_run(
                run["run_id"], pr_url=task.get("pr_url"), pr_number=task["pr_number"]
            )
            run = self.store.get_run(run["run_id"]) or run
        if task["status"] == "success":
            await self._succeeded(run, task)
            return False
        if task.get("open_question"):
            # A question: the report event goes out, and the run stays active.
            return True
        return await self._failed(run, task)

    async def _failed(self, run: dict[str, Any], task: dict[str, Any]) -> bool:
        number = task.get("tasks_task_number")
        client = self._tasks()
        if run["status"] != "active" or number is None or client is None:
            return True
        if run["retried"]:
            self.store.update_run(run["run_id"], status="paused")
            return True
        self.store.update_run(run["run_id"], retried=True)
        tasks_task = await client.by_number(int(number))
        result = await self._dispatch(run, tasks_task, client, note=" again after a failure")
        if result.get("status") == "rejected":
            self.store.update_run(run["run_id"], status="paused")
            return True
        await self._say(run, f"✗ {label(tasks_task)} failed. The run tries it once more.", task)
        return False

    async def _succeeded(self, run: dict[str, Any], task: dict[str, Any]) -> None:
        client = self._tasks()
        number = task.get("tasks_task_number")
        if client is None or number is None:
            raise TasksError("TASKS_URL is not set, so the task cannot be marked done")
        pr = task.get("pr_url") or run["pr_url"]
        finished = await client.by_number(int(number))
        done_line = f"done in {pr}" if pr else "done"
        await client.patch(
            str(finished["id"]),
            {"done": True, "notes": append_note(finished.get("notes"), done_line)},
        )
        self.store.update_run(run["run_id"], retried=False)
        if run["status"] != "active":
            await self._say(run, f"✓ {label(finished)} done. The run is {run['status']}.", task)
            return
        over = await self.over_ceiling()
        entry = await client.next_step(run["project"])
        name = entry.get("name") or run["project_name"] or run["project"]
        ready = entry.get("tasks") or []
        if not ready:
            if entry.get("open_count"):
                await self._pause(
                    run,
                    f"Run on {name} paused: every open task waits for a blocker. "
                    f"Pull request: {pr or 'none'}.",
                )
            else:
                self.store.update_run(run["run_id"], status="done")
                await self._wake(
                    run, f"Project {name} finished. Pull request: {pr or 'none'}.", task
                )
            return
        following = ready[0]
        if following.get("group_id") != finished.get("group_id"):
            phase = await client.group_name(str(entry["id"]), finished.get("group_id"))
            await self._pause(
                run,
                f"Phase {phase or 'of the project'} done on {name}. Pull request: "
                f"{pr or 'none'}. Next: {label(following)}. Say resume to continue.",
                task,
            )
            return
        if over:
            await self._pause(
                run, f"Run on {name} paused: {over} Pull request: {pr or 'none'}.", task
            )
            return
        result = await self._dispatch(run, following, client)
        if result.get("status") == "rejected":
            await self._pause(
                run,
                f"Run on {name} paused: task {label(following)} did not start "
                f"({result.get('reason')}: {result.get('message')}).",
                task,
            )
            return
        await self._say(run, f"✓ {label(finished)} — next: {label(following)}", task)

    # --- the limits --------------------------------------------------------

    async def over_ceiling(self) -> str | None:
        """A sentence when a limits window is at or over its ceiling, else None.

        A window that core cannot give is unknown, and unknown is not over.
        """
        base = self.manager.settings.core_url
        if not base:
            return None
        try:
            async with make_client() as client:
                response = await client.get(base.rstrip("/") + LIMITS_PATH)
            data = response.json() if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            data = None
        if not isinstance(data, dict) or not data.get("available"):
            logger.info({"message": "run: the limits are unknown, so the run goes on"})
            return None
        config = self.manager.config.runs
        ceilings = {
            "five_hour": config.ceiling_five_hour_pct,
            "seven_day": config.ceiling_seven_day_pct,
        }
        for name, words in WINDOWS:
            window = data.get(name)
            used = window.get("used_pct") if isinstance(window, dict) else None
            if isinstance(used, int | float) and used >= ceilings[name]:
                return f"the {words} limit is at {used:g} percent; the ceiling is {ceilings[name]}."
        return None

    # --- the events --------------------------------------------------------

    def _destination(self, run: dict[str, Any]) -> str | None:
        manager = self.manager
        row = {"notify": run["notify"], "person": run["person"]}
        return notify.destination_for(
            manager.settings, row, manager.effective_settings(run["person"])["notify"]
        )

    async def _send(
        self, run: dict[str, Any], text: str, task: dict[str, Any] | None, verbatim: bool
    ) -> None:
        destination = self._destination(run)
        if not destination:
            logger.info({"message": "run: no destination", "run_id": run["run_id"]})
            return
        await notify.send_event(
            self.manager.settings,
            destination,
            text[:MAX_TEXT],
            (task or {}).get("task_id"),
            run["workspace"],
            verbatim=verbatim,
        )

    async def _say(self, run: dict[str, Any], text: str, task: dict[str, Any] | None) -> None:
        """A verbatim line: it costs no model turn."""
        await self._send(run, text, task, verbatim=True)

    async def _wake(self, run: dict[str, Any], text: str, task: dict[str, Any] | None) -> None:
        """An event that runs one model turn."""
        await self._send(run, text + " Tell the person.", task, verbatim=False)

    async def _pause(
        self, run: dict[str, Any], text: str, task: dict[str, Any] | None = None
    ) -> None:
        self.store.update_run(run["run_id"], status="paused")
        await self._wake(run, text, task)
