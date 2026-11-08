# chores

The `chores` addon keeps the chores and an XP ledger for the members of one
household. A manager adds members and chores, and awards or deducts XP. A
member completes a chore from a kiosk screen, or asks Joshua to do it. Each
completion adds the XP of the chore to the ledger of the member, and moves a
recurring chore to its next due date.

## Words

- **Member**: a person who does chores. A member has a slug (for example
  `<slug>`), a name, an active flag, and a sort order. Each tool and each
  route that is about one member takes the slug, not the name.
- **Manager**: a person who changes the household data. On MCP, the
  gateway decides who is a manager (see
  [Add it to joshua.yaml](#add-it-to-joshuayaml)). In the web UI, a manager
  is a person who knows the `MANAGER_PIN`.
- **XP**: the points of the household. The balance of a member is the sum of
  the ledger rows of that member. No column stores it. A completion adds XP.
  A manager awards or deducts XP. The balance can go below 0.
- **`XP_PER_DOLLAR`**: the XP that equals one dollar. The addon uses it only
  to show a balance as a dollar value. When it is `0`, the addon shows no
  dollar value, and each `balance_dollars` field is `null`.

## Tools

Call `list_members` first to get the slugs. Then use the `id` of a chore
from `list_chores` with the chore tools. The frequency of a chore is
`daily`, `weekly`, `monthly`, or `one_off`. A date is `YYYY-MM-DD`. "Today"
is the date in `CHORES_TZ`.

| Tool | What it does |
|---|---|
| `list_members()` | Lists the active members, with the slug, the name, and the XP balance of each. |
| `list_chores(slug, overdue_only?)` | Lists the active chores of one member, sorted by due date, with an `overdue` flag. It does not write. |
| `get_balance(slug)` | Gives the balance and the 10 newest ledger rows of one member. |
| `complete_chore(chore_id, note?)` | Records a completion and gives the XP to the member of the chore. A recurring chore moves to its next due date. A one-off chore is retired. The cooldown applies. |
| `award_xp(slug, points, description)` | Manager. Adds XP. `points` must be more than 0. |
| `deduct_xp(slug, points, description)` | Manager. Removes XP. `points` must be more than 0. |
| `add_chore(slug, name, points, frequency, next_due_date?)` | Manager. Adds a chore to an active member. The default due date is today. |
| `update_chore(chore_id, name?, points?, frequency?, next_due_date?)` | Manager. Changes a chore. A field that you do not give does not change. |
| `retire_chore(chore_id)` | Manager. Removes a chore from the lists. The addon keeps the chore and its history. |
| `add_member(slug, name)` | Manager. Adds a member. The slug is 1 to 32 lowercase letters, digits, or hyphens, and it cannot change. |
| `set_member(slug, name?, is_active?, sort_order?)` | Manager. Changes the name, the active flag, or the sort order of a member. |

A list shows the due date of the current period. When a daily chore is
overdue, the list shows it as due today. When a weekly or monthly chore is
overdue, the list moves the date forward in full periods to the first date
on or after today. The stored date changes only on a completion.

The addon does not know who calls a tool. The `allow` and `tools` filters
of the gateway decide who can call the manager tools.

## Run it

The addon serves MCP at `POST /mcp` (streamable HTTP) on port 8000. It
answers `GET /healthz` with `{"ok": true}`. It serves the web UI at `/` and
the HTTP API at `/api` from the same port. `GET /version` gives
`{"version": "<JOSHUA_ADDONS_VERSION>"}`.

## Web UI

The web UI has two views.

- **Manager view** at `/`. Type the `MANAGER_PIN` to sign in. The sign-in
  screen shows the word in `MANAGER_LABEL`. The browser keeps the PIN for
  the tab only (session storage). Two tabs are below the header:
  - **Chores**: select an active member to see the balance, the chores, and
    the ledger. You can add, edit, and delete chores, and award or deduct
    XP. When there are no members, add a member first.
  - **Members**: the list of all members, also the inactive ones. You can
    add a member (a slug and a name), rename a member, activate or deactivate
    a member, and move a member up or down in the lists.
- **Kiosk screen** at `/<slug>`, for a tablet or a phone. It needs no PIN.
  It shows the name and the balance of one member, the active chores of
  that member sorted by due date, and the 50 newest ledger rows. An overdue
  chore has a mark. The member taps **Done** to complete a chore. After a
  tap, the button stays off for 5 seconds or for `COOLDOWN_SECONDS`, the
  longer of the two.

The kiosk screen asks for `GET /version` each minute. When the version
changes, the screen loads the page again. Thus a kiosk takes a new release
with no action.

When `MANAGER_PIN` is not set, the manager view cannot sign in, and it shows
the message "The server has no manager PIN set".

`/` and `/api` take no `ADDON_TOKEN`. The kiosk routes need no credential,
and each person who can reach the port can see the household data and
complete a chore. Use your ingress or your docker network to control who
reaches `/` and `/api`.

To reach the UI on a compose install, remove the `#` from the `ports:` block
in `docker-compose.yml`, and open `http://localhost:8000/`. On Kubernetes,
forward the port of the Service of the addon:

```
kubectl port-forward svc/<release> 8000:8000
```

Then open `http://localhost:8000/`.

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

`limit` is a whole number from 1 to 500. A value that is not valid gets 400:
a body field that the route does not know, a field of the wrong type, a bad
`limit` or flag, or a value that the addon refuses. An unknown member or
chore gets 404. A retired chore also gets 404 on `complete`.

## Settings

| Variable | Default | What it does |
|---|---|---|
| `ADDON_TOKEN` | not set | When set, each request to `/mcp` needs the header `Authorization: Bearer <ADDON_TOKEN>`. A missing or wrong token gets 401. `/healthz`, `/version`, `/`, and `/api` stay open. When not set, the addon checks no token, and the docker network is the boundary. |
| `MANAGER_PIN` | not set | The PIN of the manager routes under `/api`. The manager view sends it as `Authorization: Bearer <MANAGER_PIN>`. The addon removes the spaces at the start and the end. When it is empty, each manager route gets 503, and the addon logs a warning at startup. The addon never logs the PIN. |
| `XP_PER_DOLLAR` | `100` | The XP that equals one dollar. `0` shows no dollar value. A value that is not a whole number of 0 or more stops the startup. |
| `COOLDOWN_SECONDS` | `60` | The minimum number of seconds between two completions of one chore. `0` turns the check off. A value that is not a whole number of 0 or more stops the startup. |
| `MANAGER_LABEL` | `Parent` | The word that the manager view shows on the sign-in screen. An empty value gives the default. |
| `CHORES_TZ` | `UTC` | The time zone of the household, as an IANA name (for example `America/Chicago`). The addon uses the date in this zone for "today": for due dates, for the `overdue` flag, and for the next due date after a completion. The addon stores each timestamp in UTC. A name that is not an IANA zone stops the startup. |
| `DATABASE_URL` | not set | A full Postgres URL (for example `postgresql://user:pass@host:5432/chores`). The addon adds the `asyncpg` driver to a `postgresql://` URL. When set, the addon uses Postgres, and it does not use `CHORES_DB`. A URL that the addon cannot connect to stops the startup. |
| `CHORES_DB` | `/data/chores.db` | The SQLite file, when `DATABASE_URL` is not set. The addon makes the parent directory when it does not exist. A directory that the addon cannot make or write stops the startup. |
| `CHORES_STATIC_DIR` | `/app/addons/chores/static` | The directory of the built web UI. The image puts the UI here. When the directory does not exist, the addon serves no UI, and `/` gets 404. MCP and `/api` continue to work. |
| `JOSHUA_ADDONS_VERSION` | `dev` | The version that `GET /version` gives. The kiosk screen loads the page again when this value changes. Set it to the image tag. `docker-compose.yml` sets it from `.env`. The chart does not set it. |
| `LOG_LEVEL` | `INFO` | The log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. A name that is not a level gives `INFO`. |

The error of a setting that stops the startup gives the variable name.

`docker-compose.yml` sends only `ADDON_TOKEN`, `MANAGER_PIN`,
`JOSHUA_ADDONS_VERSION`, `DATABASE_URL`, `CHORES_DB`, and `LOG_LEVEL` from
`.env` to the container. To set one of the other variables on compose, add
it to the `environment:` block of the `chores` service. On Kubernetes, put
the settings in `env:` and the secrets in `existingSecret` (see
`values.yaml`).

## Storage and backup

By default the addon keeps a SQLite file at `CHORES_DB`, on the
`chores-data` volume (compose) or on the PersistentVolumeClaim of the chart
(Kubernetes). That file is the data. Back up the volume, or copy the file
out, as you back up any other single-file database. Set `DATABASE_URL` to a
Postgres database to use no volume. The addon then keeps no data of its own,
and the backup of that database is your job, not the job of this addon.

## Add it to joshua.yaml

Use two entries on the same upstream. This is the view pattern in
`../../docs/config.md` of `joshua-ai`. The `chores` entry is for each
person: it gives the read tools and `complete_chore`. The `chores-manage`
entry is for the managers only: it gives the write tools. Replace each
`<person-id>` with a person id from `people`.

Docker Compose: the gateway reaches the addon by its compose service name.

```yaml
mcp:
  chores:
    type: http
    url: http://chores:8000/mcp
    allow: all
    tools:
      allow:
        - list_members
        - list_chores
        - get_balance
        - complete_chore
  chores-manage:
    type: http
    url: http://chores:8000/mcp
    allow:
      - <person-id>
      - <person-id>
    tools:
      allow:
        - award_xp
        - deduct_xp
        - add_chore
        - update_chore
        - retire_chore
        - add_member
        - set_member
```

Kubernetes: the gateway reaches the addon at the cluster Service DNS name,
`<release>.<ns>.svc.cluster.local`. Use this `url` in the two entries:

```yaml
    url: http://<release>.<ns>.svc.cluster.local:8000/mcp
```

Add no `headers:` block unless you set `ADDON_TOKEN`. When the token in a
`headers:` block is empty, the gateway turns the entry off and does not
connect to the addon. When you set `ADDON_TOKEN`, give the two entries the
same `headers:` block. Read `../../docs/config.md` in `joshua-ai` for the
full `mcp:` shape. See [`../../docs/install.md`](../../docs/install.md) for
the steps that apply this change, and for how to add a token.
