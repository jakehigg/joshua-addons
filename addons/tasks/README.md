# Tasks

The tasks addon for Joshua: projects, groups, and tasks over MCP, with a web
UI. This is a scaffold. It serves `/healthz` and `/mcp` (one placeholder tool,
`ping`). The data model, the real tools, the REST API, and the UI are not
built yet.

## Settings

| Variable | Meaning |
|---|---|
| `ADDON_TOKEN` | When set, `/mcp` needs `Authorization: Bearer <ADDON_TOKEN>`. |
| `DATABASE_URL` | A Postgres URL. When set, it replaces SQLite. |
| `TASKS_DB` | The SQLite file. The default is `/data/tasks.db`. |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |

Full documentation arrives with ticket P5.7.
