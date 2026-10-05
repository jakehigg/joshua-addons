"""One task at a time for each unit of work.

A lock key is ``(repo, scope)``. The scope is ``branch:<name>`` for both
``develop`` and ``rework``: a rework locks the source branch of its pull
request, so a develop and a rework on one branch never run together. A lock
belongs to one task id.
The lock stays until its task ends: the manager releases it when it records
the report, or when the worker does not start. A lock has no expiry. The
manager watches every worker to its end, and a worker that waits for an
answer can run for a long time. The locks live in the process, so a start
clears them all; restart recovery marks every task that held one as failed.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any


class LockedError(Exception):
    """The unit of work is locked by another task."""

    def __init__(self, message: str, lock_info: dict[str, Any]) -> None:
        super().__init__(message)
        self.lock_info = lock_info


@dataclass(frozen=True)
class LockInfo:
    task_id: str
    task_type: str
    branch: str | None
    started_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class LockManager:
    """In-process locks keyed by ``(repo, scope)``."""

    def __init__(self, clock: Callable[[], datetime] = _utcnow) -> None:
        self._clock = clock
        self._locks: dict[tuple[str, str], LockInfo] = {}
        self._mutex = threading.Lock()

    def acquire(
        self,
        repo: str,
        scope: str,
        task_id: str,
        task_type: str,
        branch: str | None = None,
    ) -> LockInfo:
        """Take the lock for ``task_id``. Raises LockedError when another task holds it."""
        info = LockInfo(
            task_id=task_id,
            task_type=task_type,
            branch=branch,
            started_at=self._clock().isoformat(),
        )
        with self._mutex:
            held = self._locks.get((repo, scope))
            if held is not None and held.task_id != task_id:
                raise LockedError(
                    f"{scope} in {repo} is already being worked on by task "
                    f"{held.task_id} (started {held.started_at})",
                    held.as_dict(),
                )
            self._locks[(repo, scope)] = info
        return info

    def release(self, repo: str, scope: str, task_id: str) -> None:
        """Release the lock of ``task_id``.

        A missing lock, or a lock of another task, is not changed.
        """
        with self._mutex:
            held = self._locks.get((repo, scope))
            if held is not None and held.task_id == task_id:
                del self._locks[(repo, scope)]

    def get(self, repo: str, scope: str) -> LockInfo | None:
        """The lock on ``(repo, scope)``, or None."""
        with self._mutex:
            return self._locks.get((repo, scope))

    def clear(self) -> None:
        """Release every lock."""
        with self._mutex:
            self._locks.clear()
