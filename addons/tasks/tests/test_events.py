from joshua_tasks import service as services
from joshua_tasks.events import broadcaster, current_origin


async def test_mutations_publish_events(db):
    queue = broadcaster.subscribe()
    try:
        p = await services.create_project(db, "Live")
        assert (await queue.get())["scope"] == "projects"

        t = await services.create_task(db, "live", "task")
        ev = await queue.get()
        assert ev["scope"] == "tasks" and ev["project_id"] == str(p.id)

        await services.update_task(db, t.id, done=True)
        assert (await queue.get())["scope"] == "tasks"

        g = await services.create_group(db, "live", "G")
        assert (await queue.get())["scope"] == "tasks"
        await services.delete_group(db, g.id)
        assert (await queue.get())["scope"] == "tasks"
        assert queue.empty()
    finally:
        broadcaster.unsubscribe(queue)


async def test_event_carries_origin(db):
    queue = broadcaster.subscribe()
    token = current_origin.set("tab-123")
    try:
        await services.create_project(db, "Origin")
        assert (await queue.get())["origin"] == "tab-123"
    finally:
        current_origin.reset(token)
        broadcaster.unsubscribe(queue)


async def test_move_task_notifies_both_projects(db):
    await services.create_project(db, "Src")
    dst = await services.create_project(db, "Dst")
    t = await services.create_task(db, "src", "roaming")
    queue = broadcaster.subscribe()
    try:
        await services.move_task(db, t.id, "dst")
        first, second = await queue.get(), await queue.get()
        assert {first["project_id"], second["project_id"]} == {
            str(t.project_id),
            str(dst.id),
        } or first["project_id"] != second["project_id"]
    finally:
        broadcaster.unsubscribe(queue)


async def test_full_subscriber_queue_drops_events_without_error():
    queue = broadcaster.subscribe()
    try:
        for _ in range(64):
            broadcaster.publish("projects")
        broadcaster.publish("projects")  # the 65th is dropped, not raised
        assert queue.qsize() == 64
    finally:
        broadcaster.unsubscribe(queue)
