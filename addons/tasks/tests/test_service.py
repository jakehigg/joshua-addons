import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from joshua_tasks import service as services


async def test_create_project_derives_unique_slug(db):
    p1 = await services.create_project(db, "House Chores!")
    p2 = await services.create_project(db, "House chores")
    assert p1.slug == "house-chores"
    assert p2.slug == "house-chores-2"


async def test_get_project_by_slug_and_id(db):
    p = await services.create_project(db, "Garden")
    assert (await services.get_project(db, "garden")).id == p.id
    assert (await services.get_project(db, str(p.id))).id == p.id
    with pytest.raises(services.NotFoundError):
        await services.get_project(db, "nope")


async def test_tasks_append_in_order(db):
    p = await services.create_project(db, "P")
    t1 = await services.create_task(db, "p", "first")
    t2 = await services.create_task(db, "p", "second")
    assert t2.position > t1.position
    tasks = await services.list_tasks(db, p.id)
    assert [t.title for t in tasks] == ["first", "second"]


async def test_done_toggle_sets_and_clears_done_at(db):
    await services.create_project(db, "P")
    t = await services.create_task(db, "p", "task")
    t = await services.update_task(db, t.id, done=True)
    assert t.done and t.done_at is not None
    t = await services.update_task(db, t.id, done=False)
    assert not t.done and t.done_at is None


async def test_reorder_is_authoritative(db):
    p = await services.create_project(db, "P")
    a = await services.create_task(db, "p", "a")
    b = await services.create_task(db, "p", "b")
    c = await services.create_task(db, "p", "c")
    await services.reorder_tasks(db, p.id, [c.id, a.id, b.id])
    tasks = await services.list_tasks(db, p.id)
    assert [t.title for t in tasks] == ["c", "a", "b"]


async def test_reorder_rejects_incomplete_id_set(db):
    p = await services.create_project(db, "P")
    a = await services.create_task(db, "p", "a")
    await services.create_task(db, "p", "b")
    with pytest.raises(services.ConflictError):
        await services.reorder_tasks(db, p.id, [a.id])
    with pytest.raises(services.ConflictError):
        await services.reorder_tasks(db, p.id, [a.id, uuid.uuid4()])


async def test_move_task_appends_to_target(db):
    await services.create_project(db, "Src")
    dst = await services.create_project(db, "Dst")
    t = await services.create_task(db, "src", "roaming")
    await services.create_task(db, "dst", "existing")
    moved = await services.move_task(db, t.id, "dst")
    assert moved.project_id == dst.id
    tasks = await services.list_tasks(db, "dst")
    assert [x.title for x in tasks] == ["existing", "roaming"]


async def test_archive_hides_project_from_list(db):
    await services.create_project(db, "Keep")
    await services.create_project(db, "Hide")
    await services.update_project(db, "hide", archived=True)
    names = [p["name"] for p in await services.list_projects(db)]
    assert names == ["Keep"]


async def test_delete_project_cascades_tasks(db):
    p = await services.create_project(db, "P")
    t = await services.create_task(db, "p", "task")
    task_id = t.id
    await services.delete_project(db, p.id)
    db.expire_all()  # drop the identity-map copy; the row is gone in the DB
    with pytest.raises(services.NotFoundError):
        await services.get_task(db, task_id)


async def test_get_project_by_name_case_insensitive(db):
    p = await services.create_project(db, "Paint the Hallway")
    assert (await services.get_project(db, "paint the hallway")).id == p.id
    assert (await services.get_project(db, p.id)).id == p.id


async def test_get_project_prefers_slug_over_name(db):
    a = await services.create_project(db, "Alpha")
    b = await services.create_project(db, "x")
    await services.update_project(db, b.id, name="alpha")
    # the slug "alpha" belongs to the first project even though both are named alpha
    assert (await services.get_project(db, "alpha")).id == a.id


async def test_ambiguous_project_name_conflicts(db):
    a = await services.create_project(db, "One")
    b = await services.create_project(db, "Two")
    await services.update_project(db, b.id, name="one")
    await services.update_project(db, a.id, name="ONE ")
    with pytest.raises(services.ConflictError):
        await services.get_project(db, " one ")


async def test_get_project_unknown_uuid(db):
    with pytest.raises(services.NotFoundError):
        await services.get_project(db, uuid.uuid4())
    with pytest.raises(services.NotFoundError):
        await services.get_project(db, str(uuid.uuid4()))


async def test_list_projects_counts_and_archived(db):
    p = await services.create_project(db, "P")
    await services.create_project(db, "Old")
    a = await services.create_task(db, "p", "a")
    await services.create_task(db, "p", "b")
    await services.update_task(db, a.id, done=True)
    await services.update_project(db, "old", archived=True)
    listing = await services.list_projects(db)
    assert [(x["slug"], x["task_count"], x["done_count"]) for x in listing] == [("p", 2, 1)]
    assert listing[0]["id"] == p.id
    everything = await services.list_projects(db, include_archived=True)
    assert [x["slug"] for x in everything] == ["p", "old"]
    assert everything[1]["archived_at"] is not None


async def test_update_project_rename_keeps_slug_and_unarchive(db):
    p = await services.create_project(db, "Garden")
    p = await services.update_project(db, "garden", name="  Yard  ")
    assert p.name == "Yard" and p.slug == "garden"
    p = await services.update_project(db, "garden", archived=True)
    assert p.archived_at is not None
    p = await services.update_project(db, "garden", archived=False)
    assert p.archived_at is None


async def test_slug_edge_cases(db):
    p = await services.create_project(db, "!!!")
    assert p.slug == "project"
    long = await services.create_project(db, "x" * 300)
    assert len(long.slug) == 100


async def test_list_tasks_excludes_done_when_asked(db):
    await services.create_project(db, "P")
    a = await services.create_task(db, "p", "a")
    await services.create_task(db, "p", "b")
    await services.update_task(db, a.id, done=True)
    open_only = await services.list_tasks(db, "p", include_done=False)
    assert [t.title for t in open_only] == ["b"]


async def test_update_task_fields_and_noop_done(db):
    await services.create_project(db, "P")
    t = await services.create_task(db, "p", " title ", notes="n")
    assert t.title == "title" and t.notes == "n"
    t = await services.update_task(db, t.id, title=" new ", notes="")
    assert t.title == "new" and t.notes == ""
    t = await services.update_task(db, t.id, done=False)  # already open: unchanged
    assert t.done is False and t.done_at is None
    first = (await services.update_task(db, t.id, done=True)).done_at
    again = (await services.update_task(db, t.id, done=True)).done_at
    assert first == again


async def test_delete_task_and_missing(db):
    await services.create_project(db, "P")
    t = await services.create_task(db, "p", "a")
    await services.delete_task(db, t.id)
    db.expire_all()
    with pytest.raises(services.NotFoundError):
        await services.get_task(db, t.id)
    with pytest.raises(services.NotFoundError):
        await services.delete_task(db, t.id)


async def test_create_task_unknown_group_and_project(db):
    await services.create_project(db, "P")
    with pytest.raises(services.NotFoundError):
        await services.create_task(db, "p", "a", group="nope")
    with pytest.raises(services.NotFoundError):
        await services.create_task(db, "nope", "a")


async def test_positions_append_by_1024_and_reorder_rewrites(db):
    p = await services.create_project(db, "P")
    a = await services.create_task(db, "p", "a")
    b = await services.create_task(db, "p", "b")
    assert (a.position, b.position) == (1024.0, 2048.0)
    result = await services.reorder_tasks(db, p.id, [b.id, a.id])
    assert [t.position for t in result] == [1024.0, 2048.0]
    assert [t.title for t in result] == ["b", "a"]


async def test_reorder_duplicate_ids_conflict(db):
    p = await services.create_project(db, "P")
    a = await services.create_task(db, "p", "a")
    with pytest.raises(services.ConflictError):
        await services.reorder_tasks(db, p.id, [a.id, a.id])


async def test_set_task_group_same_group_keeps_position(db):
    await services.create_project(db, "P")
    await services.create_group(db, "p", "G")
    t = await services.create_task(db, "p", "a", group="G")
    again = await services.set_task_group(db, t.id, "G")
    assert again.position == t.position


async def test_delete_project_unknown_and_move_unknown(db):
    with pytest.raises(services.NotFoundError):
        await services.delete_project(db, "nope")
    await services.create_project(db, "P")
    t = await services.create_task(db, "p", "a")
    with pytest.raises(services.NotFoundError):
        await services.move_task(db, t.id, "nope")
    with pytest.raises(services.NotFoundError):
        await services.move_task(db, uuid.uuid4(), "p")


# --- goals, health, and roadmap order (issue #37) -------------------------------


async def _age(db, project, days: int) -> None:
    """Pretend the project was created ``days`` ago."""
    project.created_at = datetime.now(UTC) - timedelta(days=days)
    await db.commit()


async def test_list_without_goal_has_no_health(db):
    p = await services.create_project(db, "Household")
    await services.create_task(db, "household", "a")
    await _age(db, p, 60)
    [summary] = await services.list_projects(db)
    assert summary["health"] is None and summary["goal"] is None
    assert summary["next_task"].title == "a"


async def test_goal_project_health_moves_with_progress_and_dates(db):
    p = await services.create_project(db, "Hall", goal="  Painted  ")
    assert p.goal == "Painted"
    assert (await services.project_summary(db, "hall"))["health"] is None  # no tasks yet
    a = await services.create_task(db, "hall", "a")
    b = await services.create_task(db, "hall", "b")
    assert (await services.project_summary(db, "hall"))["health"] == "on_track"
    await _age(db, p, services.STALLED_AFTER_DAYS)
    assert (await services.project_summary(db, "hall"))["health"] == "stalled"
    await services.update_task(db, a.id, done=True)
    summary = await services.project_summary(db, "hall")
    assert summary["health"] == "on_track" and summary["percent"] == 50
    assert summary["last_progress_at"] is not None
    await services.update_project(db, "hall", target_date=date.today() - timedelta(days=1))
    assert (await services.project_summary(db, "hall"))["health"] == "overdue"
    await services.update_task(db, b.id, done=True)
    summary = await services.project_summary(db, "hall")
    assert summary["health"] == "ready_to_complete" and summary["percent"] == 100
    assert summary["next_task"] is None
    await services.complete_project(db, "hall")
    assert (await services.project_summary(db, "hall"))["health"] == "done"


async def test_target_date_alone_gives_health(db):
    await services.create_project(db, "Trip", target_date=date.today() + timedelta(days=30))
    # a project with no tasks has nothing to be on track about
    assert (await services.project_summary(db, "trip"))["health"] is None
    t = await services.create_task(db, "trip", "pack")
    assert (await services.project_summary(db, "trip"))["health"] == "on_track"
    await services.update_project(db, "trip", target_date=date.today() - timedelta(days=1))
    assert (await services.project_summary(db, "trip"))["health"] == "overdue"
    # overdue needs open work; without a goal there is no completion nudge either
    await services.update_task(db, t.id, done=True)
    assert (await services.project_summary(db, "trip"))["health"] == "on_track"


async def test_update_project_goal_is_presence_sensitive(db):
    p = await services.create_project(db, "P", goal="g", target_date=date(2030, 1, 1))
    p = await services.update_project(db, "p", name="Q")
    assert p.goal == "g" and p.target_date == date(2030, 1, 1)
    p = await services.update_project(db, "p", goal="", target_date=None)
    assert p.goal is None and p.target_date is None


async def test_set_project_status_rules(db):
    await services.create_project(db, "P")
    t = await services.create_task(db, "p", "a")
    with pytest.raises(services.ConflictError, match="1 task\\(s\\) are still open"):
        await services.set_project_status(db, "p", "done")
    with pytest.raises(services.InvalidError):
        await services.set_project_status(db, "p", "finished")
    p = await services.set_project_status(db, "p", "paused")
    assert p.status == "paused" and p.completed_at is None
    p = await services.set_project_status(db, "p", "closed")
    assert p.status == "closed" and p.completed_at is not None
    assert [x["slug"] for x in await services.list_projects(db, include_finished=False)] == []
    assert [x["slug"] for x in await services.list_projects(db)] == ["p"]
    p = await services.set_project_status(db, "p", "active")
    assert p.status == "active" and p.completed_at is None
    await services.update_task(db, t.id, done=True)
    p = await services.complete_project(db, "p")
    assert p.status == "done" and p.completed_at is not None


async def test_roadmap_order_is_phases_then_backlog(db):
    p = await services.create_project(db, "P")
    late = await services.create_task(db, "p", "backlog")
    g1 = await services.create_group(db, "p", "Phase 1")
    g2 = await services.create_group(db, "p", "Phase 2")
    second = await services.create_task(db, "p", "p2", group=g2.id)
    first = await services.create_task(db, "p", "p1", group=g1.id)
    tasks = await services.list_tasks(db, p.id)
    groups = [g1, g2]
    assert [t.title for t in services.roadmap_order(tasks, groups)] == ["p1", "p2", "backlog"]
    await services.reorder_groups(db, p.id, [g2.id, g1.id])
    await db.refresh(g1)
    await db.refresh(g2)
    tasks = await services.list_tasks(db, p.id)
    assert [t.title for t in services.roadmap_order(tasks, groups)] == ["p2", "p1", "backlog"]
    assert (await services.project_summary(db, "p"))["next_task"].id == second.id
    await services.update_task(db, second.id, done=True)
    assert (await services.project_summary(db, "p"))["next_task"].id == first.id
    await services.update_task(db, first.id, done=True)
    assert (await services.project_summary(db, "p"))["next_task"].id == late.id


async def test_next_steps_filters_and_limits(db):
    await services.create_project(db, "Go")
    await services.create_project(db, "Paused")
    await services.create_project(db, "Empty")
    await services.create_project(db, "Hidden")
    for title in ("a", "b", "c", "d"):
        await services.create_task(db, "go", title)
    await services.create_task(db, "paused", "x")
    await services.create_task(db, "hidden", "y")
    await services.set_project_status(db, "paused", "paused")
    await services.update_project(db, "hidden", archived=True)
    steps = await services.next_steps(db, limit=2)
    assert [(s["slug"], [t.title for t in s["tasks"]]) for s in steps] == [("go", ["a", "b"])]
    [named] = await services.next_steps(db, "paused")
    assert named["status"] == "paused" and [t.title for t in named["tasks"]] == ["x"]
    [none] = await services.next_steps(db, "go", limit=0)
    assert none["tasks"] == []


async def test_task_size_due_date_assignee_rules(db):
    await services.create_project(db, "P")
    t = await services.create_task(
        db, "p", "t", size=" l ", due_date=date(2030, 2, 2), assignee="  Sam "
    )
    assert (t.size, t.due_date, t.assignee) == ("L", date(2030, 2, 2), "Sam")
    t = await services.update_task(db, t.id, title="renamed")
    assert (t.size, t.due_date, t.assignee) == ("L", date(2030, 2, 2), "Sam")
    t = await services.update_task(db, t.id, size=None, due_date=None, assignee="")
    assert (t.size, t.due_date, t.assignee) == (None, None, None)
    with pytest.raises(services.InvalidError, match="size must be one of"):
        await services.update_task(db, t.id, size="huge")
    with pytest.raises(services.InvalidError):
        await services.create_task(db, "p", "x", size="tiny")
    assert services.normalize_size("") is None and services.normalize_size("xs") == "XS"
    assert repr(services.UNSET) == "UNSET"


async def test_group_target_date(db):
    await services.create_project(db, "P")
    g = await services.create_group(db, "p", "Phase 1")
    g = await services.update_group(db, g.id, target_date=date(2030, 3, 1))
    assert g.target_date == date(2030, 3, 1)
    g = await services.update_group(db, g.id, name="Phase one")
    assert g.target_date == date(2030, 3, 1) and g.name == "Phase one"
    [listed] = await services.list_groups(db, "p")
    assert listed["target_date"] == date(2030, 3, 1) and listed["percent"] == 0
    g = await services.update_group(db, g.id, target_date=None)
    assert g.target_date is None


# --- search (issue #40) -------------------------------------------------------


async def test_search_matches_titles_notes_names_and_goals(db):
    await services.create_project(db, "Hallway", goal="Hallway painted")
    await services.create_project(db, "Garden", goal="Beds ready for paint")
    await services.create_task(db, "hallway", "Buy paint")
    await services.create_task(db, "hallway", "Tape edges", notes="no PAINT on the trim")
    await services.create_task(db, "garden", "Dig")
    found = await services.search(db, "paint")
    assert [p["slug"] for p in found["projects"]] == ["hallway", "garden"]
    assert [f["task"].title for f in found["tasks"]] == ["Buy paint", "Tape edges"]
    assert [f["project"]["slug"] for f in found["tasks"]] == ["hallway", "hallway"]
    assert found["projects"][0]["percent"] == 0 and found["projects"][0]["open_count"] == 2
    assert found["tasks"][0]["group"] is None


async def test_search_is_case_insensitive_and_follows_roadmap_order(db):
    await services.create_project(db, "P")
    g = await services.create_group(db, "p", "Phase 1")
    await services.create_task(db, "p", "backlog paint")
    await services.create_task(db, "p", "phase PAINT", group=g.id)
    found = await services.search(db, "  PaInT  ")
    assert [f["task"].title for f in found["tasks"]] == ["phase PAINT", "backlog paint"]
    assert found["tasks"][0]["group"].name == "Phase 1"
    assert found["tasks"][1]["group"] is None


async def test_search_skips_done_tasks_and_finished_projects_by_default(db):
    await services.create_project(db, "Paint the hallway", goal="g")
    t = await services.create_task(db, "paint-the-hallway", "paint the wall")
    await services.create_task(db, "paint-the-hallway", "paint the door")
    await services.update_task(db, t.id, done=True)
    open_only = await services.search(db, "paint")
    assert [f["task"].title for f in open_only["tasks"]] == ["paint the door"]
    everything = await services.search(db, "paint", include_done=True)
    assert [f["task"].title for f in everything["tasks"]] == ["paint the wall", "paint the door"]

    await services.set_project_status(db, "paint-the-hallway", "closed")
    assert await services.search(db, "paint") == {"projects": [], "tasks": []}
    closed = await services.search(db, "paint", include_done=True)
    assert [p["slug"] for p in closed["projects"]] == ["paint-the-hallway"]
    assert len(closed["tasks"]) == 2


async def test_search_project_filter_and_archived_projects(db):
    await services.create_project(db, "Mine")
    await services.create_project(db, "Yours")
    await services.create_project(db, "Old")
    await services.create_task(db, "mine", "paint a wall")
    await services.create_task(db, "yours", "paint a door")
    await services.create_task(db, "old", "paint the shed")
    await services.update_project(db, "old", archived=True)

    both = await services.search(db, "paint")
    assert [f["project"]["slug"] for f in both["tasks"]] == ["mine", "yours"]
    one = await services.search(db, "paint", project="yours")
    assert [f["task"].title for f in one["tasks"]] == ["paint a door"]
    # the archived project is out, with its tasks, even by name
    hidden = await services.search(db, "old", include_done=True)
    assert hidden == {"projects": [], "tasks": []}
    with pytest.raises(services.NotFoundError):
        await services.search(db, "paint", project="nope")


async def test_search_empty_query_no_match_and_limit(db):
    await services.create_project(db, "Paint")
    for n in range(3):
        await services.create_task(db, "paint", f"paint {n}")
    assert await services.search(db, "   ") == {"projects": [], "tasks": []}
    assert await services.search(db, "zebra") == {"projects": [], "tasks": []}
    assert await services.search(db, "paint", limit=0) == {"projects": [], "tasks": []}
    limited = await services.search(db, "paint", limit=2)
    assert [f["task"].title for f in limited["tasks"]] == ["paint 0", "paint 1"]
    assert len(limited["projects"]) == 1
    assert services.SEARCH_LIMIT == 20
