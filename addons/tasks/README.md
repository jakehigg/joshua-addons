# tasks

A small task manager for a household. Joshua manages projects, groups, and
tasks over MCP. People use the same lists in a web UI. Both see one database,
and the web UI updates live when Joshua or another tab makes a change.

It is a checklist, not a project-management suite: projects, an ordered task
list, optional groups inside a project, notes on a task, and done or not done.

## Tools

| Tool | What it does |
|---|---|
| `list_projects` | The projects that are not archived, with open and done counts. Call it first. |
| `create_project` | Makes a project. |
| `rename_project` | Changes the name of a project. |
| `archive_project` | Hides a project. Its tasks are kept. |
| `list_tasks` | The tasks of one project, in order, with their groups. |
| `create_task` | Adds a task to a project, at the end of the list or of a group. |
| `update_task` | Changes the title or the notes of a task. |
| `set_task_done` | Marks a task done or not done. |
| `reorder_tasks` | Sets the order of the tasks in one list (the project root list or one group). |
| `move_task` | Moves a task to another project. |
| `create_group` | Adds a group to a project. |
| `rename_group` | Changes the name of a group. |
| `delete_group` | Deletes a group. Its tasks move to the root list. No task is deleted. |
| `set_task_group` | Moves a task into a group, or out of all groups. |

A project argument takes the project id, its slug, or its name. A group
argument takes the group id or its name (case does not matter).

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
