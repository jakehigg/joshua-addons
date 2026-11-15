"""The work directories of the workers, on storage that a restart keeps.

When ``DEVELOPER_WORK_DIR`` is set, the manager has the work storage mounted
there, and each worker gets one directory of it at ``/work``:
``<work dir>/<task id>``. The worker clones into that directory and commits
there, so its commits stay when the worker, the manager, or a push fails.
The runtime mounts only the directory of the task into the worker, never the
whole storage, so a worker cannot read the work of another task.

The rules:

- A task gets its directory before its worker starts (``prepare``). A resumed
  task keeps its own directory. A new task of the same person, for the same
  repository and branch as an earlier task that did not end ``success``,
  takes the directory of that task, so the worker continues from the commits
  in it.
- A task that ends ``success`` loses its directory (``drop``). A task that
  ends in any other status keeps it.
- The sweeper (``sweep``) deletes a kept directory when its task ended more
  than ``work.keep_failed`` days ago, and logs each deletion. It never
  deletes the directory of an active task.

The manager finds a directory only from a task id in its store, never from
a request or from a file in the directory, because the worker can write every
file in it.
"""

from __future__ import annotations

import os
import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from joshua_developer.log import get_logger
from joshua_developer.store import ACTIVE_STATUSES, TaskStore, parse_time

logger = get_logger("joshua_developer.workspaces")

# A directory is open to its owner only: the manager and the worker are uid 1000.
DIR_MODE = 0o700
# The checkout of a worker in its directory (the worker's /work/repo).
CHECKOUT = "repo"


class Workspaces:
    """The work directories under ``root``. ``root`` None turns them off."""

    def __init__(
        self,
        root: Path | None,
        store: TaskStore,
        keep_days: int,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.root = root
        self.store = store
        self.keep_s = keep_days * 86400
        self.now = now

    @property
    def enabled(self) -> bool:
        return self.root is not None

    def path(self, task_id: str) -> Path:
        """The directory of a task. The task id comes from the store."""
        assert self.root is not None
        return self.root / task_id

    def has_checkout(self, task_id: str) -> bool:
        """True when the directory of a task holds a git checkout that a worker left."""
        if self.root is None:
            return False
        return (self.path(task_id) / CHECKOUT / ".git").is_dir()

    def prepare(self, task: dict[str, Any]) -> Path | None:
        """Give ``task`` (the full row) its directory. Returns the path, or None when off.

        Raises OSError when the directory cannot be made.
        """
        if self.root is None:
            return None
        task_id = task["task_id"]
        path = self.path(task_id)
        if path.is_dir():
            logger.info({"message": "workspace kept for the task", "task_id": task_id})
            return path
        earlier = self._kept_for(task)
        if earlier is not None:
            os.rename(self.path(earlier), path)
            logger.info(
                {"message": "workspace reused", "task_id": task_id, "from_task_id": earlier}
            )
            return path
        self.root.mkdir(parents=True, exist_ok=True)
        path.mkdir(mode=DIR_MODE)
        path.chmod(DIR_MODE)
        logger.info({"message": "workspace made", "task_id": task_id})
        return path

    def _kept_for(self, task: dict[str, Any]) -> str | None:
        """The newest earlier task of the same person, repository, and branch with a directory.

        Only a task of the same person: the git config in a kept checkout is
        written by a worker, and the next worker runs git with its own token.
        """
        branch = task.get("branch_name")
        if not branch:
            return None
        kept = self.store.find_kept_tasks(task["person"], task["repo"], branch, task["task_id"])
        for task_id in kept:
            if self.path(task_id).is_dir():
                return task_id
        return None

    def drop(self, task_id: str) -> bool:
        """Delete the directory of a task. Returns True when it is gone."""
        if self.root is None:
            return True
        path = self.path(task_id)
        if not path.exists():
            return True
        try:
            shutil.rmtree(path)
        except OSError as exc:
            # The sweeper tries again: a task that ended success has no age limit.
            logger.warning(
                {
                    "message": "workspace not deleted",
                    "task_id": task_id,
                    "error_type": type(exc).__name__,
                }
            )
            return False
        logger.info({"message": "workspace deleted", "task_id": task_id, "reason": "success"})
        return True

    def sweep(self) -> int:
        """Delete each directory that no task needs. Returns the count.

        A directory goes when its task ended ``success``, when its task ended
        more than ``work.keep_failed`` days ago, or when the store has no task
        for it and it is older than that. A directory of an active task stays.
        A name that is not a task id, such as ``lost+found`` at the root of a
        file system, is never touched.
        """
        if self.root is None or not self.root.is_dir():
            return 0
        limit = self.now() - self.keep_s
        count = 0
        for path in self.root.iterdir():
            if path.is_symlink() or not path.is_dir() or not _is_task_id(path.name):
                continue
            task = self.store.get_task(path.name)
            if task is not None and task["status"] in ACTIVE_STATUSES:
                continue
            reason = self._sweep_reason(path, task, limit)
            if reason is None:
                continue
            try:
                shutil.rmtree(path)
            except OSError as exc:
                logger.warning(
                    {
                        "message": "workspace not deleted",
                        "task_id": path.name,
                        "error_type": type(exc).__name__,
                    }
                )
                continue
            count += 1
            logger.info({"message": "workspace deleted", "task_id": path.name, "reason": reason})
        return count

    def _sweep_reason(self, path: Path, task: dict[str, Any] | None, limit: float) -> str | None:
        if task is not None and task["status"] == "success":
            return "success"
        ended = parse_time(task["completed_at"]) if task is not None else None
        try:
            stamp = ended.timestamp() if ended is not None else path.stat().st_mtime
        except OSError:
            return None
        if stamp < limit:
            return "expired" if task is not None else "no_task"
        return None


def _is_task_id(name: str) -> bool:
    """True when ``name`` is a task id: a UUID in its canonical form."""
    try:
        return str(uuid.UUID(name)) == name
    except ValueError:
        return False
