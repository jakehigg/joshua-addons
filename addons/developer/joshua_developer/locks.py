"""One task at a time for each unit of work.

A lock key is ``(repo, scope)``. The scope is ``branch:<name>`` for
``develop`` and ``pr:<number>`` for ``rework``. A lock belongs to one task id.
The locks live in the process, so a start clears them all; restart recovery
marks every task that held one as failed.

A lock also has an expiry, as a safety net for a task whose end the manager
never sees. An acquire over an expired lock takes it, and the old owner then
finds its lock stolen when it releases.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

DEFAULT_TTL_S = 3600


class LockedError(Exception):
    """The unit of work is locked by another task."""

    def __init__(self, message: str, lock_info: dict[str, Any]) -> None:
        super().__init__(message)
        self.lock_info = lock_info


class LockStolenError(Exception):
    """The lock now belongs to another task."""


@dataclass(frozen=True)
class LockInfo:
    task_id: str
    task_type: str
    branch: str | None
    started_at: str
    expires_at: str

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

    def _expired(self, info: LockInfo) -> bool:
        return datetime.fromisoformat(info.expires_at) <= self._clock()

    def acquire(
        self,
        repo: str,
        scope: str,
        task_id: str,
        task_type: str,
        branch: str | None = None,
        ttl_s: int = DEFAULT_TTL_S,
    ) -> LockInfo:
        """Take the lock for ``task_id``. Raises LockedError when another task holds it."""
        now = self._clock()
        info = LockInfo(
            task_id=task_id,
            task_type=task_type,
            branch=branch,
            started_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=ttl_s)).isoformat(),
        )
        with self._mutex:
            held = self._locks.get((repo, scope))
            if held is not None and held.task_id != task_id and not self._expired(held):
                raise LockedError(
                    f"{scope} in {repo} is already being worked on by task "
                    f"{held.task_id} (started {held.started_at})",
                    held.as_dict(),
                )
            self._locks[(repo, scope)] = info
        return info

    def release(self, repo: str, scope: str, task_id: str) -> None:
        """Release the lock of ``task_id``.

        A missing lock is not an error. Raises LockStolenError when another
        task now holds the lock, and leaves that lock in place.
        """
        with self._mutex:
            held = self._locks.get((repo, scope))
            if held is None:
                return
            if held.task_id != task_id:
                raise LockStolenError(
                    f"cannot release the lock for {scope} in {repo}: task "
                    f"{held.task_id} holds it now"
                )
            del self._locks[(repo, scope)]

    def get(self, repo: str, scope: str) -> LockInfo | None:
        """The lock on ``(repo, scope)``, or None. An expired lock counts as none."""
        with self._mutex:
            held = self._locks.get((repo, scope))
        if held is None or self._expired(held):
            return None
        return held

    def clear(self) -> None:
        """Release every lock."""
        with self._mutex:
            self._locks.clear()
