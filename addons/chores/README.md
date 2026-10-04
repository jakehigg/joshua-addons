# chores

The `chores` addon keeps the chores of one household. Each member has a list
of chores and an XP balance. When a member completes a chore, the addon
records the completion, adds XP to the member's ledger, and moves the chore
to its next due date. A manager adds members and chores, and awards or
deducts XP. Joshua uses the addon over MCP. The members use a web UI on a
kiosk screen or a phone.

This version has the server, the storage, the MCP tools, and the HTTP API.
The web UI comes in a later change.

## Run it

The addon serves MCP at `POST /mcp` (streamable HTTP) on port 8000. It
answers `GET /healthz` with `{"ok": true}`, serves the HTTP API at `/api`,
and serves the web UI at `/` from the same port. `GET /version` gives
`{"version": "<JOSHUA_ADDONS_VERSION>"}`, so that a kiosk can reload after a
deploy.

## Tools

Each tool that is about a member takes the member's slug. Call
`list_members` first to get the slugs. A "manager" tool changes the
household data. The addon does not know who calls. Use the `allow` and
`tools` filters of the gateway to limit the manager tools to some people.

| Tool | What it does |
|---|---|
| `list_members()` | Lists the active members, with the slug, the name, and the XP balance of each. |
| `list_chores(slug, overdue_only?)` | Lists the active chores of one member, sorted by due date. It does not write. |
| `complete_chore(chore_id, note?)` | Records a completion and gives the XP to the member. A recurring chore moves to its next due date. A one-off chore is retired. The cooldown applies. |
| `get_balance(slug)` | Gives the balance and the 10 newest ledger rows of one member. |
| `award_xp(slug, points, description)` | Manager. Adds XP. `points` must be more than 0. |
| `deduct_xp(slug, points, description)` | Manager. Removes XP. `points` must be more than 0. The balance can go below 0. |
| `add_chore(slug, name, points, frequency, next_due_date?)` | Manager. Adds a chore. The default due date is today. |
| `update_chore(chore_id, name?, points?, frequency?, next_due_date?)` | Manager. Changes a chore. |
| `retire_chore(chore_id)` | Manager. Removes a chore from the lists. The addon keeps the chore and its history. |
| `add_member(slug, name)` | Manager. Adds a member. |
| `set_member(slug, name?, is_active?, sort_order?)` | Manager. Changes the name, the active flag, or the sort order of a member. |
| `import_data(version, members, chores?, completions?, transactions?)` | Manager. Loads data in bulk from an export. See [Import](#import). |

The frequency of a chore is `daily`, `weekly`, `monthly`, or `one_off`. A
date is `YYYY-MM-DD`. "Today" is the date in `CHORES_TZ`.

## HTTP API

The web UI uses the HTTP API under `/api`. The UI and the API have the same
origin. Each body is JSON. An error body is `{"detail": "<message>"}`.

Open routes need no credential. The ingress or the docker network is the
boundary for them, as for the UI.

| Route | What it does |
|---|---|
| `GET /api/settings` | Gives `manager_label`, `xp_per_dollar`, `cooldown_seconds`, and `tz`. |
| `GET /api/version` | Gives `{"version": ...}`, the same as `GET /version`. |
| `GET /api/members?include_inactive=true` | Lists the members with their balances. Without the flag, only the active members. |
| `GET /api/members/{slug}` | Gives one member with the balance. |
| `GET /api/members/{slug}/ledger?limit=50` | Lists the ledger rows of one member, newest first. |
| `GET /api/chores?slug=&overdue_only=true` | Gives `today` and the active chores, sorted by due date. Without `slug`, the chores of all members. It does not write. |
| `POST /api/chores/{id}/complete` | Records a completion. The body `{"note": ...}` is optional. The cooldown gives 409 and a `Retry-After` header. |

A manager route needs the header `Authorization: Bearer <MANAGER_PIN>`. A
missing or wrong PIN gets 401. When `MANAGER_PIN` is not set, each manager
route gets 503, and the open routes continue to work. `ADDON_TOKEN` does not
apply to `/api`.

| Route | What it does |
|---|---|
| `POST /api/members` | Adds a member: `{"slug", "name"}`. Gives 201. |
| `PATCH /api/members/{slug}` | Changes a member: `{"name"?, "is_active"?, "sort_order"?}`. |
| `POST /api/chores` | Adds a chore: `{"slug", "name", "points", "frequency", "next_due_date"?}`. Gives 201. |
| `PATCH /api/chores/{id}` | Changes a chore: `{"name"?, "points"?, "frequency"?, "next_due_date"?, "is_active"?}`. `PUT` does the same. |
| `DELETE /api/chores/{id}` | Retires a chore. The addon keeps the chore and its history. Gives 204. |
| `POST /api/members/{slug}/award` | Adds XP: `{"points", "description"}`. |
| `POST /api/members/{slug}/deduct` | Removes XP: `{"points", "description"}`. |
| `GET /api/transactions?slug=&limit=100` | Lists the ledger rows, newest first. Without `slug`, the rows of all members. |

A body field that the route does not know gets 400. An unknown member or
chore gets 404.

## Settings

| Variable | Default | What it does |
|---|---|---|
| `CHORES_DB` | `/data/chores.db` | The SQLite file path, when `DATABASE_URL` is not set. It is the same as the `chores-data` volume mount in `docker-compose.yml`. |
| `DATABASE_URL` | not set | A full Postgres URL (for example `postgresql://user:pass@host:5432/chores`). When set, the addon uses Postgres, and the `chores-data` volume is not used. |
| `ADDON_TOKEN` | not set | When set, each request to `/mcp` needs the header `Authorization: Bearer <ADDON_TOKEN>`. A missing or wrong token gets 401. `/healthz` stays open. When not set, the addon checks no token, and the docker network is the boundary. |
| `MANAGER_PIN` | not set | The PIN of the manager routes under `/api`. The manager UI sends it as `Authorization: Bearer <MANAGER_PIN>`. When it is not set, each manager route gets 503, and the addon logs a warning at startup. The addon never logs the PIN. |
| `JOSHUA_ADDONS_VERSION` | `dev` | The version that `GET /version` gives. Set it to the image tag. |
| `CHORES_STATIC_DIR` | `/app/addons/chores/static` | The directory of the built web UI. The image puts the UI here. When the directory does not exist, the addon serves no UI at `/`. |
| `XP_PER_DOLLAR` | `100` | The XP that equals one dollar, for the dollar value of a balance. `0` shows no dollar value. |
| `COOLDOWN_SECONDS` | `60` | The minimum number of seconds between two completions of one chore. `0` turns the check off. |
| `MANAGER_LABEL` | `Parent` | The word that the UI shows for a manager. |
| `CHORES_TZ` | `UTC` | The time zone of the household, as an IANA name (for example `America/Chicago`). The addon uses the date in this zone for "today": for due dates, and for the next due date after a completion. The addon stores each timestamp in UTC. |
| `LOG_LEVEL` | `INFO` | The log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |

A setting that is not valid stops the addon at startup, with an error that
gives the variable name.

## Import

`import_data` loads members, chores, completions, and ledger rows from an
export, with their original timestamps. A second import of the same data
changes nothing. [`docs/import.md`](docs/import.md) gives the format.

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
