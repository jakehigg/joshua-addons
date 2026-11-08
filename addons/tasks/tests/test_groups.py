import uuid

import pytest
from joshua_tasks import service as services


async def _project_with_tasks(db, names=("a", "b", "c")):
    p = await services.create_project(db, "P")
    tasks = [await services.create_task(db, "p", n) for n in names]
    return p, tasks


async def test_group_crud_and_resolution(db):
    p, _ = await _project_with_tasks(db)
    g = await services.create_group(db, "p", "Prep Work")
    assert (await services.resolve_group(db, p.id, "prep work")).id == g.id
    assert (await services.resolve_group(db, p.id, str(g.id))).id == g.id
    g = await services.rename_group(db, g.id, "Painting")
    assert g.name == "Painting"
    with pytest.raises(services.NotFoundError):
        await services.resolve_group(db, p.id, "nope")


async def test_ambiguous_group_name_conflicts(db):
    p, _ = await _project_with_tasks(db)
    await services.create_group(db, "p", "Same")
    await services.create_group(db, "p", "same")
    with pytest.raises(services.ConflictError):
        await services.resolve_group(db, p.id, "SAME")


async def test_create_task_in_group_and_counts(db):
    await _project_with_tasks(db)
    g = await services.create_group(db, "p", "G")
    t = await services.create_task(db, "p", "grouped", group="G")
    assert t.group_id == g.id
    await services.update_task(db, t.id, done=True)
    groups = await services.list_groups(db, "p")
    assert groups[0]["task_count"] == 1 and groups[0]["done_count"] == 1


async def test_set_task_group_moves_to_end(db):
    _, (a, b, c) = await _project_with_tasks(db)
    g = await services.create_group(db, "p", "G")
    await services.set_task_group(db, a.id, "G")
    await services.set_task_group(db, b.id, str(g.id))
    grouped = [t for t in await services.list_tasks(db, "p") if t.group_id == g.id]
    assert [t.title for t in grouped] == ["a", "b"]
    # ungroup lands at the end of the root list
    await services.set_task_group(db, a.id, None)
    root = [t for t in await services.list_tasks(db, "p") if t.group_id is None]
    assert [t.title for t in root] == ["c", "a"]


async def test_reorder_within_group_and_move_in(db):
    p, (a, b, c) = await _project_with_tasks(db)
    await services.create_group(db, "p", "G")
    await services.set_task_group(db, a.id, "G")
    # listing c moves it into the group; order becomes c, a
    result = await services.reorder_tasks(db, p.id, [c.id, a.id], group="G")
    assert [t.title for t in result] == ["c", "a"]
    root = [t for t in await services.list_tasks(db, "p") if t.group_id is None]
    assert [t.title for t in root] == ["b"]


async def test_reorder_root_ignores_grouped_tasks(db):
    p, (a, b, c) = await _project_with_tasks(db)
    await services.create_group(db, "p", "G")
    await services.set_task_group(db, c.id, "G")
    # root reorder with just the root tasks — identical to the old behavior
    result = await services.reorder_tasks(db, p.id, [b.id, a.id])
    assert [t.title for t in result] == ["b", "a"]


async def test_reorder_conflicts(db):
    p, (a, b, c) = await _project_with_tasks(db)
    await services.create_group(db, "p", "G")
    with pytest.raises(services.ConflictError):
        # missing b and c from the root container
        await services.reorder_tasks(db, p.id, [a.id])
    with pytest.raises(services.ConflictError):
        await services.reorder_tasks(db, p.id, [a.id, b.id, c.id, uuid.uuid4()])


async def test_delete_group_ungroups_members_at_end(db):
    p, (a, b, c) = await _project_with_tasks(db)
    pid = p.id
    g = await services.create_group(db, "p", "G")
    await services.set_task_group(db, a.id, "G")
    await services.set_task_group(db, b.id, "G")
    await services.delete_group(db, g.id)
    db.expire_all()
    tasks = await services.list_tasks(db, pid)
    assert all(t.group_id is None for t in tasks)
    assert [t.title for t in tasks] == ["c", "a", "b"]


async def test_reorder_groups(db):
    p, _ = await _project_with_tasks(db)
    g1 = await services.create_group(db, "p", "One")
    g2 = await services.create_group(db, "p", "Two")
    groups = await services.reorder_groups(db, p.id, [g2.id, g1.id])
    assert [g["name"] for g in groups] == ["Two", "One"]
    with pytest.raises(services.ConflictError):
        await services.reorder_groups(db, p.id, [g1.id])


async def test_move_task_cross_project_clears_group(db):
    await _project_with_tasks(db, names=("a",))
    dst = await services.create_project(db, "Dst")
    await services.create_group(db, "p", "G")
    t = await services.create_task(db, "p", "grouped", group="G")
    moved = await services.move_task(db, t.id, "dst")
    assert moved.project_id == dst.id and moved.group_id is None


async def test_group_lookups_across_projects_and_missing(db):
    p, _ = await _project_with_tasks(db)
    other = await services.create_project(db, "Other")
    g = await services.create_group(db, "p", "G")
    with pytest.raises(services.NotFoundError):
        await services.resolve_group(db, other.id, g.id)
    with pytest.raises(services.NotFoundError):
        await services.resolve_group(db, p.id, uuid.uuid4())
    with pytest.raises(services.NotFoundError):
        await services.get_group(db, uuid.uuid4())
    with pytest.raises(services.NotFoundError):
        await services.rename_group(db, uuid.uuid4(), "x")
    with pytest.raises(services.NotFoundError):
        await services.delete_group(db, uuid.uuid4())
    with pytest.raises(services.NotFoundError):
        await services.list_groups(db, "nope")


async def test_groups_append_in_order_and_list_counts(db):
    await _project_with_tasks(db)
    g1 = await services.create_group(db, "p", "One")
    g2 = await services.create_group(db, "p", "Two")
    assert (g1.position, g2.position) == (1024.0, 2048.0)
    groups = await services.list_groups(db, "p")
    assert [(g["name"], g["task_count"], g["done_count"]) for g in groups] == [
        ("One", 0, 0),
        ("Two", 0, 0),
    ]


async def test_delete_group_appends_after_existing_root_tasks(db):
    p, (a, b, c) = await _project_with_tasks(db)
    g = await services.create_group(db, "p", "G")
    t = await services.create_task(db, "p", "in-g", group="G")
    last_root, pid = c.position, p.id
    t_id = t.id
    await services.delete_group(db, g.id)
    db.expire_all()
    tasks = await services.list_tasks(db, pid)
    assert [x.title for x in tasks] == ["a", "b", "c", "in-g"]
    assert tasks[-1].position == last_root + 1024.0
    assert tasks[-1].id == t_id


async def test_reorder_with_unknown_group_not_found(db):
    p, (a, *_) = await _project_with_tasks(db)
    with pytest.raises(services.NotFoundError):
        await services.reorder_tasks(db, p.id, [a.id], group="nope")


async def test_reorder_groups_unknown_project(db):
    with pytest.raises(services.NotFoundError):
        await services.reorder_groups(db, "nope", [])
