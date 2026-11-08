import uuid

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
