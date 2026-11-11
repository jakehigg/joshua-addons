# tasks

A small task manager for a household. Joshua manages projects, groups, and
tasks over MCP. People use the same lists in a web UI. Both see one database,
and the web UI updates live when Joshua or another tab makes a change.

It is a checklist with a direction, not a project-management suite:
projects, an ordered task list, optional groups inside a project, notes on a
task, and done or not done. A project can have a goal, and the addon then
tracks progress toward it.

## Projects, goals, and phases

- A **project** is a named list of tasks. A project with a **goal** is meant
  to be completed: its tasks are the steps to the goal, it shows a percent
  done, and it gets a health signal. A project without a goal is a plain
  list, for example a backlog or a household list. It never completes and is
  never nagged.
- A project has a **status**: `active`, `paused`, `done`, or `closed`.
  `complete_project` sets `done` and refuses while a task is open. A project
  can be `closed` with open tasks. `paused` keeps a project out of
  `next_steps`.
- A project and a group can have a **target date**. Nothing enforces it; a
  passed date with open tasks makes the project `overdue`.
- The **groups** of a project are its phases, in their own order. The tasks
  in no group are the **backlog** and come after the phases. The order of the
  tasks is the order to do them in, and the first open task in that order is
  the project's **next task**.
- A task can have a **size** (`XS`, `S`, `M`, `L`, `XL`), a **due date**, and
  an **assignee** (a name; the addon keeps no roster). All three are optional.
- Each task has a **number**, for example `24`, that a person can say out
  loud. One count runs over all the projects. The addon gives the number when
  it makes the task, and the number then never changes and is never given to
  another task. A task keeps its number when it moves to another group or to
  another project.

The computed fields are not stored. `percent` is done tasks over all tasks.
`health` is one of `on_track`, `stalled` (a goal project with no task done in
14 days, or a project whose every open task waits for a blocker), `overdue`,
`ready_to_complete` (every task done), or the status of a paused, done, or
closed project. A project with no tasks, or a list with no goal and no date,
has no health.

## Blocking

A task can wait for other tasks. `block_task` makes one link: the task cannot
start before the blocker is done. It takes the number of each task, or its id,
so a person can say "27 is blocked by 24". A task can wait for any number of
tasks and any number of tasks can wait for it, and a blocker can be in another
project.

- `blocked` is true while at least one blocker is open. `blockers` and
  `blocking` are the tasks on each side, with the number, the id, the title,
  the done state, and the project of each. All three are computed on every
  read and never stored, so the last open blocker going done unblocks the
  task. There is nothing to clear by hand.
- A **blocked task is never a next step**: it never comes back from
  `next_steps` and it is never a project's `next_task`. A project whose whole
  remaining work waits keeps its place in `next_steps`, with no task and the
  health `stalled`. A passed target date is louder: such a project reads as
  `overdue`.
- A task cannot block itself, and a link that makes a loop is refused with an
  error that names the tasks in the loop.
- `set_task_done` reports the tasks it unblocked. A task that goes done while
  a blocker of its own is still open is accepted, with a warning that names
  the open blockers: the work sometimes runs ahead of the links.

## Tools

| Tool | What it does |
|---|---|
| `list_projects` | The projects that are not archived, with counts, percent, health, and the next task. Call it first. `include_finished` adds done and closed projects. |
| `create_project` | Makes a project, with an optional goal and target date. |
| `rename_project` | Changes the name of a project. |
| `set_project_goal` | Sets or removes the goal and the target date of a project. |
| `complete_project` | Marks a project `done`. Refuses while a task is open. |
| `set_project_status` | Sets `active`, `paused`, or `closed`. |
| `archive_project` | Hides a project. Its tasks are kept. |
| `next_steps` | The next open tasks of every active project, or of one project, in roadmap order. |
| `search_tasks` | Finds the projects and the tasks that hold a word. Use it before you add a task, to see whether it is there already. |
| `list_tasks` | The tasks of one project in roadmap order: each group with its tasks, then the backlog. |
| `create_task` | Adds a task to a project, at the end of the backlog or of a group, with an optional size, due date, and assignee. |
| `update_task` | Changes the title, notes, size, due date, or assignee of a task. |
| `set_task_done` | Marks a task done or not done. Reports the tasks it unblocked, and warns when a blocker of its own is still open. |
| `block_task` | Makes a task wait for another task. The blocker can be in another project. |
| `unblock_task` | Removes one blocking link. |
| `reorder_tasks` | Sets the order of the tasks in one list (the backlog or one group). |
| `move_task` | Moves a task to another project. |
| `create_group` | Adds a group to a project. |
| `rename_group` | Changes the name of a group. |
| `set_group_target_date` | Sets or clears the target date of a group. |
| `delete_group` | Deletes a group. Its tasks move to the backlog. No task is deleted. |
| `set_task_group` | Moves a task into a group, or out of all groups. |

A project argument takes the project id, its slug, or its name. A group
argument takes the group id or its name (case does not matter). A task
argument, and a blocker argument, takes the task number (`24`) or the task
id. A date argument is `YYYY-MM-DD`; an empty string clears a date. Every
result that holds a task gives its number. The REST API finds a task by its
number at `GET /api/tasks/by-number/<number>`.

`search_tasks` matches a substring, and case does not matter. A project
matches on its name or its goal, and a task on its title or its notes. The
result keeps the two kinds apart: the projects first, then the tasks. The
projects come oldest first, and the tasks come project by project, in
roadmap order inside a project. Only open tasks and unfinished projects are
searched, until `include_done` is true. An archived project is never
searched. `project` keeps the task match in one project, and `limit` caps
each list. The same search is at `GET /api/search`, with `query`,
`include_done`, `project`, and `limit` as query parameters.

The REST API has the links as well: `POST /api/tasks/<task_id>/blockers` with
`{"blocker_id": "<id>"}` makes one link, and
`DELETE /api/tasks/<task_id>/blockers/<blocker_id>` removes it. Both answer
with the task, and every task carries `blocked`, `blockers`, and `blocking`.

## Run it

The addon serves MCP at `POST /mcp` (streamable HTTP) on port 8000, answers
`GET /healthz` with `{"ok": true}`, and serves the web UI and its REST API
from the same port.

```bash
cp .env.example .env
docker compose up -d
```

## Web UI

Open the addon's port (or the chart's ingress host) in a browser.

The home page is one card for each project: the name, the goal, the percent
done with a bar, the done and the open count, the health, the target date,
and the next task. A project that needs a person comes first: overdue, then
stalled, then ready to complete, then on track, then a plain list, then a
project with no tasks, then paused. The nearer target date comes first in a
tie. A card opens the project at `/p/<slug>`. "Show finished" adds the done
and the closed projects.

The search box on the home page finds a project by its name or its goal, and
a task by its title or its notes. It keeps the two kinds apart: the
projects, each as its card, then the tasks, each with its project and its
group. A task opens its project page at the task. Only open tasks and
unfinished projects match until "Include done" is on. On a project page, the
Search link in the bar goes back to the box.

A project page shows the goal (click it to edit), the progress bar, the
health, the target date, and the status; the Complete button is enabled when
every task is done. Phases come first, then the backlog. The first open task
that waits for nothing carries an "up next" mark. Drag a task by its handle
to reorder it, or drag it into a group. Click a title to edit it in place;
the "⋯" button on a task opens its size, due date, and assignee. Done tasks
sink to the bottom of their list.

A task row shows the number, the title, the size, the due date, the
assignee, the done state, and the blocked state. A task with a note also
shows a "note" control. The row keeps the note out of view, also after a
reload, so a long note does not fill the screen. Click the control to open
the note of that task. The other tasks stay closed. Click an open note to
edit it in place.

A task that waits carries a "blocked" mark and names the tasks it waits for,
each with its number and a link to that task in its own project. A task also names the tasks that
wait for it. The "×" on a blocker removes that link. Joshua makes a link, with
`block_task`.

Each task row shows its number before the title. Select the number to copy
it. A link to a task is `/p/<slug>#task-<number>`, for example
`/p/hallway#task-24`, and it opens the project page at that task. A link
that holds the task id in place of the number still opens the same task.

The web UI and its REST API under `/api` have no login. Keep the port on a
trusted network, or put an ingress with its own access control in front.
`/mcp` has its own guard (`ADDON_TOKEN`).

Live updates use one server-sent event stream in the process. Run one
replica only.

The home page has its own unit tests: the order of the cards, the toggle,
and the search. They run on node's own test runner and need no package.
Run `npm test` in `addons/tasks/frontend`.

## Settings

| Variable | Meaning |
|---|---|
| `ADDON_TOKEN` | When set, `/mcp` needs `Authorization: Bearer <ADDON_TOKEN>`. |
| `DATABASE_URL` | A Postgres URL. When set, it replaces SQLite. |
| `TASKS_DB` | The SQLite file. The default is `/data/tasks.db`. |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |

## Storage and backup

By default the addon keeps a SQLite file at `TASKS_DB`, on the `tasks-data`
volume (compose) or the chart's PersistentVolumeClaim (Kubernetes). That file
**is** the data. Back up the volume, or copy the file out. Point
`DATABASE_URL` at a Postgres instance instead to drop the volume; backing up
that database is then your job, not this addon's.

## Add it to joshua.yaml

Docker Compose: the gateway reaches the addon by its compose network name.

```yaml
mcp:
  tasks:
    type: http
    url: http://tasks:8000/mcp
    allow: all
```

Kubernetes: the gateway reaches the addon at the cluster Service DNS name,
`<release>.<namespace>.svc.cluster.local`.

```yaml
mcp:
  tasks:
    type: http
    url: http://tasks.<ns>.svc.cluster.local:8000/mcp
    allow: all
```

Add no `headers:` block unless you set `ADDON_TOKEN`. An empty token renders
as `Authorization: Bearer ` with nothing after it, and the gateway then never
connects to the addon. See the pantry README and `docs/config.md` in
`joshua-ai` for the full `mcp:` shape.
