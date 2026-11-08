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

The computed fields are not stored. `percent` is done tasks over all tasks.
`health` is one of `on_track`, `stalled` (a goal project with no task done in
14 days), `overdue`, `ready_to_complete` (every task done), or the status of a
paused, done, or closed project. A project with no tasks, or a list with no
goal and no date, has no health.

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
| `list_tasks` | The tasks of one project in roadmap order: each group with its tasks, then the backlog. |
| `create_task` | Adds a task to a project, at the end of the backlog or of a group, with an optional size, due date, and assignee. |
| `update_task` | Changes the title, notes, size, due date, or assignee of a task. |
| `set_task_done` | Marks a task done or not done. |
| `reorder_tasks` | Sets the order of the tasks in one list (the backlog or one group). |
| `move_task` | Moves a task to another project. |
| `create_group` | Adds a group to a project. |
| `rename_group` | Changes the name of a group. |
| `set_group_target_date` | Sets or clears the target date of a group. |
| `delete_group` | Deletes a group. Its tasks move to the backlog. No task is deleted. |
| `set_task_group` | Moves a task into a group, or out of all groups. |

A project argument takes the project id, its slug, or its name. A group
argument takes the group id or its name (case does not matter). A date
argument is `YYYY-MM-DD`; an empty string clears a date.

## Run it

The addon serves MCP at `POST /mcp` (streamable HTTP) on port 8000, answers
`GET /healthz` with `{"ok": true}`, and serves the web UI and its REST API
from the same port.

```bash
cp .env.example .env
docker compose up -d
```

## Web UI

Open the addon's port (or the chart's ingress host) in a browser. Pick a
project at the top. Drag a task by its handle to reorder it, or drag it into
a group. Click a title or a note to edit it in place. Done tasks sink to the
bottom of their list.

The web UI and its REST API under `/api` have no login. Keep the port on a
trusted network, or put an ingress with its own access control in front.
`/mcp` has its own guard (`ADDON_TOKEN`).

Live updates use one server-sent event stream in the process. Run one
replica only.

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
