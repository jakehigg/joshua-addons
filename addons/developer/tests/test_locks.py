"""The in-process lock: one task for each (repo, scope)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from joshua_developer.locks import LockedError, LockManager, LockStolenError

REPO = "github.com/example-home/sandbox"
SCOPE = "branch:feature"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def locks(clock: Clock) -> LockManager:
    return LockManager(clock=clock)


def test_acquire_and_get(locks: LockManager) -> None:
    locks.acquire(REPO, SCOPE, "task-1", "develop", "feature")
    info = locks.get(REPO, SCOPE)
    assert info is not None
    assert info.task_id == "task-1"
    assert info.task_type == "develop"
    assert info.branch == "feature"


def test_a_second_task_is_locked_out(locks: LockManager) -> None:
    locks.acquire(REPO, SCOPE, "task-1", "develop", "feature")
    with pytest.raises(LockedError) as exc:
        locks.acquire(REPO, SCOPE, "task-2", "develop", "feature")
    assert exc.value.lock_info["task_id"] == "task-1"
    assert "task-1" in str(exc.value)


def test_the_owner_can_acquire_again(locks: LockManager) -> None:
    locks.acquire(REPO, SCOPE, "task-1", "develop")
    locks.acquire(REPO, SCOPE, "task-1", "develop")


def test_scopes_and_repos_are_independent(locks: LockManager) -> None:
    locks.acquire(REPO, "branch:feature", "task-1", "develop")
    locks.acquire(REPO, "pr:7", "task-2", "rework")
    locks.acquire("github.com/example-home/other", "branch:feature", "task-3", "develop")
    assert locks.get(REPO, "branch:feature").task_id == "task-1"  # type: ignore[union-attr]
    assert locks.get(REPO, "pr:7").task_id == "task-2"  # type: ignore[union-attr]


def test_release(locks: LockManager) -> None:
    locks.acquire(REPO, SCOPE, "task-1", "develop")
    locks.release(REPO, SCOPE, "task-1")
    assert locks.get(REPO, SCOPE) is None


def test_release_of_a_missing_lock_does_nothing(locks: LockManager) -> None:
    locks.release(REPO, SCOPE, "task-1")


def test_an_expired_lock_is_taken_and_the_old_owner_finds_it_stolen(
    locks: LockManager, clock: Clock
) -> None:
    locks.acquire(REPO, SCOPE, "task-1", "develop", ttl_s=60)
    clock.now += timedelta(seconds=61)
    assert locks.get(REPO, SCOPE) is None
    locks.acquire(REPO, SCOPE, "task-2", "develop")
    with pytest.raises(LockStolenError):
        locks.release(REPO, SCOPE, "task-1")
    assert locks.get(REPO, SCOPE).task_id == "task-2"  # type: ignore[union-attr]


def test_clear(locks: LockManager) -> None:
    locks.acquire(REPO, SCOPE, "task-1", "develop")
    locks.clear()
    assert locks.get(REPO, SCOPE) is None
    locks.acquire(REPO, SCOPE, "task-2", "develop")
