"""The short task number: one sequence, given once, and never changed.

Runs on every engine the ``engine`` fixture offers, so the sequence is proved
on SQLite and, with ``TEST_DATABASE_URL``, on Postgres.
"""

from __future__ import annotations

import uuid

import pytest
from joshua_tasks import service as services
from joshua_tasks.models import TASK_NUMBER_ID, Task, TaskNumber
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError


async def test_a_new_task_gets_the_next_number(db) -> None:
    await services.create_project(db, "P")
    first = await services.create_task(db, "p", "a")
    second = await services.create_task(db, "p", "b")
    assert first.number == 1
    assert second.number == 2


async def test_the_sequence_is_one_for_every_project(db) -> None:
    await services.create_project(db, "One")
    await services.create_project(db, "Two")
    a = await services.create_task(db, "one", "a")
    b = await services.create_task(db, "two", "b")
    c = await services.create_task(db, "one", "c")
    assert [a.number, b.number, c.number] == [1, 2, 3]


async def test_every_number_is_used_once(db) -> None:
    await services.create_project(db, "P")
    for i in range(5):
        await services.create_task(db, "p", f"task {i}")
    numbers = (await db.scalars(select(Task.number))).all()
    assert sorted(numbers) == [1, 2, 3, 4, 5]
    assert len(set(numbers)) == len(numbers)


async def test_two_tasks_cannot_hold_the_same_number(db) -> None:
    """The unique index refuses a duplicate, whatever a writer tries."""
    await services.create_project(db, "P")
    a = await services.create_task(db, "p", "a")
    b = await services.create_task(db, "p", "b")
    with pytest.raises(IntegrityError):
        await db.execute(update(Task).where(Task.id == b.id).values(number=a.number))
        await db.commit()
    await db.rollback()


async def test_a_deleted_number_is_never_given_again(db) -> None:
    await services.create_project(db, "P")
    a = await services.create_task(db, "p", "a")
    b = await services.create_task(db, "p", "b")
    await services.delete_task(db, b.id)
    await services.delete_task(db, a.id)
    fresh = await services.create_task(db, "p", "c")
    assert fresh.number == 3


async def test_the_number_survives_every_change(db) -> None:
    await services.create_project(db, "Src")
    await services.create_project(db, "Dst")
    task = await services.create_task(db, "src", "a")
    number = task.number
    group = await services.create_group(db, "src", "G")
    task = await services.update_task(db, task.id, title="renamed", notes="n")
    assert task.number == number
    task = await services.set_task_group(db, task.id, group.id)
    assert task.number == number
    task = await services.update_task(db, task.id, done=True)
    assert task.number == number
    task = await services.update_task(db, task.id, done=False)
    assert task.number == number
    task = await services.move_task(db, task.id, "dst")
    assert task.number == number
    await services.reorder_tasks(db, "dst", [task.id])
    assert (await services.get_task(db, task.id)).number == number


async def test_resolve_task_takes_the_number_or_the_uuid(db) -> None:
    await services.create_project(db, "P")
    task = await services.create_task(db, "p", "a")
    assert (await services.resolve_task(db, task.number)).id == task.id
    assert (await services.resolve_task(db, str(task.number))).id == task.id
    assert (await services.resolve_task(db, task.id)).id == task.id
    assert (await services.resolve_task(db, str(task.id))).id == task.id


async def test_an_unknown_task_says_what_it_did_not_find(db) -> None:
    await services.create_project(db, "P")
    with pytest.raises(services.NotFoundError, match="task not found: 404"):
        await services.resolve_task(db, 404)
    missing = uuid.uuid4()
    with pytest.raises(services.NotFoundError, match=f"task not found: {missing}"):
        await services.resolve_task(db, missing)
    with pytest.raises(services.NotFoundError, match="task not found: junk"):
        await services.resolve_task(db, "junk")


async def test_every_task_tool_argument_takes_the_number(db) -> None:
    await services.create_project(db, "P")
    task = await services.create_task(db, "p", "a")
    number = task.number
    assert (await services.update_task(db, number, title="by number")).title == "by number"
    assert (await services.update_task(db, number, done=True)).done is True
    assert (await services.set_task_group(db, number, None)).group_id is None
    await services.reorder_tasks(db, "p", [number])
    await services.delete_task(db, number)
    with pytest.raises(services.NotFoundError):
        await services.resolve_task(db, number)


async def test_a_number_of_another_project_is_not_reordered_in(db) -> None:
    await services.create_project(db, "One")
    await services.create_project(db, "Two")
    await services.create_task(db, "one", "a")
    other = await services.create_task(db, "two", "b")
    with pytest.raises(services.ConflictError):
        await services.reorder_tasks(db, "one", [other.number])


async def test_the_sequence_row_holds_the_last_number(db) -> None:
    await services.create_project(db, "P")
    await services.create_task(db, "p", "a")
    await services.create_task(db, "p", "b")
    counter = await db.get(TaskNumber, TASK_NUMBER_ID)
    assert counter is not None and counter.last_number == 2
