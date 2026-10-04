# chores

The `chores` addon keeps the chores of one household. Each member has a list
of chores and an XP balance. When a member completes a chore, the addon
records the completion, adds XP to the member's ledger, and moves the chore
to its next due date. A manager adds members and chores, and awards or
deducts XP. Joshua uses the addon over MCP. The members use a web UI on a
kiosk screen or a phone.

This version is a scaffold. It has the server, the storage, and one tool,
`ping`. The chore tools, the REST API, and the web UI come in later changes.

## Run it

The addon serves MCP at `POST /mcp` (streamable HTTP) on port 8000. It
answers `GET /healthz` with `{"ok": true}`, and serves the web UI at `/`
from the same port.

## Tools

| Tool | What it does |
|---|---|
| `ping` | Returns `{"ok": true}`. Use it to make sure that the gateway can reach the addon. |

## Settings

| Variable | Default | What it does |
|---|---|---|
| `CHORES_DB` | `/data/chores.db` | The SQLite file path, when `DATABASE_URL` is not set. It is the same as the `chores-data` volume mount in `docker-compose.yml`. |
| `DATABASE_URL` | not set | A full Postgres URL (for example `postgresql://user:pass@host:5432/chores`). When set, the addon uses Postgres, and the `chores-data` volume is not used. |
| `ADDON_TOKEN` | not set | When set, each request to `/mcp` needs the header `Authorization: Bearer <ADDON_TOKEN>`. A missing or wrong token gets 401. `/healthz` stays open. When not set, the addon checks no token, and the docker network is the boundary. |
| `MANAGER_PIN` | not set | The PIN that the manager UI sends as `Authorization: Bearer <MANAGER_PIN>` on each write route under `/api`. When it is empty, the addon refuses each write route. The REST API comes in a later change. |
| `CHORES_STATIC_DIR` | `/app/addons/chores/static` | The directory of the built web UI. The image puts the UI here. When the directory does not exist, the addon serves no UI at `/`. |
| `LOG_LEVEL` | `INFO` | The log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |

## Add it to joshua.yaml

Docker Compose: the gateway reaches the addon by its compose service name.

```yaml
mcp:
  chores:
    type: http
    url: http://chores:8000/mcp
    allow: all
```

Kubernetes: the gateway reaches the addon at the cluster Service DNS name,
`<release>.<namespace>.svc.cluster.local`.

Do not add a `headers:` block unless you set `ADDON_TOKEN`. Read
`../../docs/config.md` in `joshua-ai` for the full `mcp:` shape.
