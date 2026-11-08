"""Blocking links between tasks (issue #52), at the service level."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from joshua_tasks import service as services
from joshua_tasks.models import TaskBlocker
from sqlalchemy import func, select


async def _link_count(db) -> int:
    return (await db.execute(select(func.count()).select_from(TaskBlocker))).scalar_one()


async def test_block_and_unblock_one_task(db):
    await services.create_project(db, "Hall")
    paint = await services.create_task(db, "hall", "Buy the paint")
    trim = await services.create_task(db, "hall", "Paint the trim")

    blocked = await services.block_task(db, trim.id, paint.id)
    assert blocked.blocked is True
    assert [r["title"] for r in blocked.blockers] == ["Buy the paint"]
    assert blocked.blockers[0]["number"] == paint.number
    assert blocked.blockers[0]["project"] == {"name": "Hall", "slug": "hall"}

    [other] = [t for t in await services.list_tasks(db, "hall") if t.id == paint.id]
    assert other.blocked is False
    assert [r["title"] for r in other.blocking] == ["Paint the trim"]

    free = await services.unblock_task(db, trim.id, paint.id)
    assert free.blocked is False and free.blockers == []
    assert await _link_count(db) == 0


async def test_a_number_names_either_end_of_a_link(db):
    """Each argument takes the number or the id, as a person says it: 27 is blocked by 24."""
    await services.create_project(db, "Hall")
    paint = await services.create_task(db, "hall", "Buy the paint")
    trim = await services.create_task(db, "hall", "Paint the trim")

    blocked = await services.block_task(db, trim.number, paint.number)
    assert blocked.id == trim.id and blocked.blocked is True
    assert [r["number"] for r in blocked.blockers] == [paint.number]

    free = await services.unblock_task(db, str(trim.number), str(paint.number))
    assert free.id == trim.id and free.blockers == []
    assert await _link_count(db) == 0

    report = await services.set_task_done(db, str(paint.number), True)
    assert report["task"].id == paint.id and report["task"].done is True


async def test_the_same_link_twice_stores_one_and_unknown_removal_is_quiet(db):
    await services.create_project(db, "Hall")
    paint = await services.create_task(db, "hall", "Buy the paint")
    trim = await services.create_task(db, "hall", "Paint the trim")
    await services.block_task(db, trim.id, paint.id)
    again = await services.block_task(db, trim.id, paint.id)
    assert len(again.blockers) == 1
    assert await _link_count(db) == 1
    # a link that is not there
    still = await services.unblock_task(db, paint.id, trim.id)
    assert still.blockers == []
    assert await _link_count(db) == 1


async def test_a_task_cannot_block_itself(db):
    await services.create_project(db, "Hall")
    t = await services.create_task(db, "hall", "Buy the paint")
    with pytest.raises(services.ConflictError, match="cannot block itself: Buy the paint"):
        await services.block_task(db, t.id, t.id)
    assert await _link_count(db) == 0


async def test_a_loop_is_refused_and_the_error_names_the_tasks(db):
    await services.create_project(db, "Hall")
    a = await services.create_task(db, "hall", "a")
    b = await services.create_task(db, "hall", "b")
    c = await services.create_task(db, "hall", "c")
    await services.block_task(db, b.id, a.id)  # b waits for a
    await services.block_task(db, c.id, b.id)  # c waits for b
    with pytest.raises(services.ConflictError) as exc:
        await services.block_task(db, a.id, c.id)  # would close the loop
    assert str(exc.value) == "this link makes a loop: a blocks b blocks c blocks a"
    assert await _link_count(db) == 2


async def test_a_blocker_in_another_project_works(db):
    await services.create_project(db, "Hall")
    await services.create_project(db, "Garage")
    keys = await services.create_task(db, "garage", "Collect the keys")
    trim = await services.create_task(db, "hall", "Paint the trim")
    blocked = await services.block_task(db, trim.id, keys.id)
    assert blocked.blocked is True
    assert blocked.blockers[0]["project"] == {"name": "Garage", "slug": "garage"}
    assert (await services.project_summary(db, "hall"))["next_task"] is None
    garage = await services.project_summary(db, "garage")
    assert garage["next_task"].title == "Collect the keys"
    assert [r["title"] for r in garage["next_task"].blocking] == ["Paint the trim"]


async def test_the_last_blocker_going_done_unblocks_the_task(db):
    await services.create_project(db, "Hall")
    paint = await services.create_task(db, "hall", "Buy the paint")
    brush = await services.create_task(db, "hall", "Find a brush")
    trim = await services.create_task(db, "hall", "Paint the trim")
    await services.block_task(db, trim.id, paint.id)
    await services.block_task(db, trim.id, brush.id)

    await services.update_task(db, paint.id, done=True)
    [waiting] = [t for t in await services.list_tasks(db, "hall") if t.id == trim.id]
    assert waiting.blocked is True  # one blocker is still open

    await services.update_task(db, brush.id, done=True)
    [free] = [t for t in await services.list_tasks(db, "hall") if t.id == trim.id]
    assert free.blocked is False
    assert [r["done"] for r in free.blockers] == [True, True]

    # a blocker that reopens blocks the task again
    await services.update_task(db, brush.id, done=False)
    [again] = [t for t in await services.list_tasks(db, "hall") if t.id == trim.id]
    assert again.blocked is True


async def test_next_task_and_next_steps_skip_a_blocked_task(db):
    await services.create_project(db, "Hall", goal="A painted hall")
    trim = await services.create_task(db, "hall", "Paint the trim")
    paint = await services.create_task(db, "hall", "Buy the paint")
    assert (await services.project_summary(db, "hall"))["next_task"].title == "Paint the trim"

    await services.block_task(db, trim.id, paint.id)
    summary = await services.project_summary(db, "hall")
    assert summary["next_task"].title == "Buy the paint"
    assert summary["open_count"] == 2  # the counts do not change
    [steps] = await services.next_steps(db, limit=3)
    assert [t.title for t in steps["tasks"]] == ["Buy the paint"]

    await services.update_task(db, paint.id, done=True)
    [steps] = await services.next_steps(db, limit=3)
    assert [t.title for t in steps["tasks"]] == ["Paint the trim"]
    assert steps["tasks"][0].blocked is False


async def test_a_project_whose_whole_remaining_work_waits_is_stalled(db):
    await services.create_project(db, "Hall", goal="A painted hall")
    await services.create_project(db, "Garage")
    keys = await services.create_task(db, "garage", "Collect the keys")
    trim = await services.create_task(db, "hall", "Paint the trim")
    assert (await services.project_summary(db, "hall"))["health"] == "on_track"

    await services.block_task(db, trim.id, keys.id)
    summary = await services.project_summary(db, "hall")
    assert summary["health"] == "stalled" and summary["next_task"] is None
    # the project keeps its place in next_steps, with nothing to do
    [hall] = [s for s in await services.next_steps(db) if s["slug"] == "hall"]
    assert hall["tasks"] == [] and hall["health"] == "stalled"

    await services.update_task(db, keys.id, done=True)
    assert (await services.project_summary(db, "hall"))["health"] == "on_track"


async def test_an_overdue_project_stays_overdue_while_it_waits(db):
    """A date that passed is louder than a wait, so overdue comes first."""
    await services.create_project(
        db, "Hall", goal="A painted hall", target_date=date.today() - timedelta(days=1)
    )
    await services.create_project(db, "Garage")
    keys = await services.create_task(db, "garage", "Collect the keys")
    trim = await services.create_task(db, "hall", "Paint the trim")
    await services.block_task(db, trim.id, keys.id)
    assert (await services.project_summary(db, "hall"))["health"] == "overdue"
    # a plain list has no goal and no date, so it has no health to report
    assert (await services.project_summary(db, "garage"))["health"] is None


async def test_set_task_done_reports_the_tasks_it_unblocked(db):
    await services.create_project(db, "Hall")
    paint = await services.create_task(db, "hall", "Buy the paint")
    brush = await services.create_task(db, "hall", "Find a brush")
    trim = await services.create_task(db, "hall", "Paint the trim")
    await services.block_task(db, trim.id, paint.id)
    await services.block_task(db, trim.id, brush.id)

    report = await services.set_task_done(db, paint.id, True)
    assert report["task"].done is True
    assert report["unblocked"] == []  # the brush is still open
    assert report["warning"] is None

    report = await services.set_task_done(db, brush.id, True)
    assert [r["title"] for r in report["unblocked"]] == ["Paint the trim"]
    assert report["warning"] is None


async def test_done_while_blocked_warns_and_names_the_open_blockers(db):
    await services.create_project(db, "Hall")
    paint = await services.create_task(db, "hall", "Buy the paint")
    brush = await services.create_task(db, "hall", "Find a brush")
    trim = await services.create_task(db, "hall", "Paint the trim")
    await services.block_task(db, trim.id, paint.id)
    await services.block_task(db, trim.id, brush.id)
    await services.update_task(db, brush.id, done=True)

    report = await services.set_task_done(db, trim.id, True)
    assert report["task"].done is True  # the work is never refused
    assert report["warning"] == "this task went done while it still waits for: Buy the paint"
    assert report["unblocked"] == []
    # reopening the task warns about nothing
    assert (await services.set_task_done(db, trim.id, False))["warning"] is None


async def test_search_carries_blocked_and_the_blockers(db):
    await services.create_project(db, "Hall")
    paint = await services.create_task(db, "hall", "Buy the paint")
    trim = await services.create_task(db, "hall", "Paint the trim")
    await services.block_task(db, trim.id, paint.id)
    found = await services.search(db, "paint")
    by_title = {f["task"].title: f for f in found["tasks"]}
    assert by_title["Paint the trim"]["task"].blocked is True
    assert [r["title"] for r in by_title["Paint the trim"]["task"].blockers] == ["Buy the paint"]
    assert by_title["Buy the paint"]["task"].blocked is False
    assert by_title["Paint the trim"]["project"]["next_task"].title == "Buy the paint"


async def test_deleting_a_task_removes_its_links(db):
    await services.create_project(db, "Hall")
    paint = await services.create_task(db, "hall", "Buy the paint")
    trim = await services.create_task(db, "hall", "Paint the trim")
    await services.block_task(db, trim.id, paint.id)
    await services.delete_task(db, paint.id)
    assert await _link_count(db) == 0
    [free] = await services.list_tasks(db, "hall")
    assert free.blocked is False and free.blockers == []


async def test_blocking_an_unknown_task_is_not_found(db):
    await services.create_project(db, "Hall")
    t = await services.create_task(db, "hall", "Buy the paint")
    with pytest.raises(services.NotFoundError):
        await services.block_task(db, t.id, uuid.uuid4())
    with pytest.raises(services.NotFoundError):
        await services.unblock_task(db, uuid.uuid4(), t.id)
