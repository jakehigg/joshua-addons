# pantry

The `pantry` addon tracks a household's grocery pantry from receipts: what is
on hand, when it runs out, what it costs, and which exact product to reorder.

## The receipt workflow

Read a receipt, then call `record_purchase` once with every line on it and
the date printed on the receipt. A line's name matches an existing item, one
of its aliases, or a close fuzzy match; a name that matches nothing creates a
new tracked item, so a receipt name never needs to already match the pantry.
Recording the same item again on the same date merges into the one purchase
record for that day, and a later call can still fill in a SKU or UPC the
first call left out.

Everything else reads that state back (`get_inventory`, `get_item_history`,
`get_price_stats`, `resolve_product`) or makes a small manual correction to
it (`add_alias`, `set_preferred_product`, `delete_purchase`). Use
`consume_items` when something runs out between shopping trips, so
depletion estimates stay honest. For a first-time load from another system,
use `import_data` (see "Bulk import" below) instead of replaying old
receipts through `record_purchase`.

## Tools

| Tool | What it does |
|---|---|
| `record_purchase` | Record one receipt: every item bought, on one purchase date. |
| `get_inventory` | List every tracked item's status, category, and estimated depletion. |
| `get_item_history` | Purchase history and frequency for one item. |
| `resolve_product` | The exact product an item resolves to, if the household has settled on one. |
| `set_preferred_product` | Pin, or clear, the exact product an item resolves to. |
| `get_price_stats` | Cost statistics for one item: overall, per store, and per product. |
| `get_item_cost` | Average and most recent cost for one or more items, e.g. to price a planned trip. |
| `consume_items` | Mark items consumed, for example after a meal. |
| `set_preferred_store` | Set or clear the store an item is usually bought at (a display hint only). |
| `set_purchase_cost` | Attach a price to a purchase that already exists. |
| `set_purchase_store` | Tag which store one or more existing purchases came from. |
| `delete_purchase` | Delete a single purchase record without removing the item. |
| `delete_item` | Permanently delete a tracked item and all its history. Needs `confirm=true`. |
| `add_alias` | Teach the pantry that one name refers to an existing item. |
| `list_aliases` | List alternate names on file for one item, or for every item. |
| `import_data` | Load pantry history in bulk: items, purchases, and consumption events. |

## Bulk import

`import_data` loads items, purchases, and consumption events in bulk, for a
first-time load from another system or a legacy export. Read
[`docs/import.md`](docs/import.md) for the file format. `scripts/pantry_import.py`,
in the repository root, reads a JSON file and drives the chunking for you:

```
uv run python scripts/pantry_import.py my_export.json --url http://localhost:8000/mcp
```

## Run it

The addon serves MCP at `POST /mcp` (streamable HTTP) on port 8000, and answers
`GET /healthz` with `{"ok": true}`.

## Settings

| Variable | Default | What it does |
|---|---|---|
| `PANTRY_DB` | `/data/pantry.db` | The SQLite file path, when `DATABASE_URL` is not set. Matches the `pantry-data` volume mount in `docker-compose.yml`. |
| `DATABASE_URL` | not set | A full Postgres URL (e.g. `postgresql://user:pass@host:5432/pantry`). When set, the addon uses Postgres instead of the bundled SQLite file, and the `pantry-data` volume goes unused. |
| `ADDON_TOKEN` | not set | When set, every request to `/mcp` needs the header `Authorization: Bearer <ADDON_TOKEN>`. A missing or wrong token gets 401. `/healthz` stays open. When `ADDON_TOKEN` is not set, the addon checks no token; the docker network is the boundary. |
| `LOG_LEVEL` | `INFO` | The log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |

## Storage and backup

By default the addon keeps a SQLite file at `PANTRY_DB`, on the `pantry-data`
volume (compose) or the chart's PersistentVolumeClaim (Kubernetes). That file
**is** the data. Back up the volume, or copy the file out, the same way you
would back up any other single-file database. Point `DATABASE_URL` at a
Postgres instance instead to drop the volume; the addon then keeps no data of
its own. Backing up that data is then the same job as backing up any other
database in your Postgres instance: it is on you, not this addon.

## Add it to joshua.yaml

Docker Compose: the gateway reaches the addon by its compose network name.

```yaml
mcp:
  pantry:
    type: http
    url: http://pantry:8000/mcp
    allow: all
```

Kubernetes: the gateway reaches the addon at the cluster Service DNS name,
`<release>.<namespace>.svc.cluster.local`.

```yaml
mcp:
  pantry:
    type: http
    url: http://pantry.<ns>.svc.cluster.local:8000/mcp
    allow: all
```

Add no `headers:` block unless you set `ADDON_TOKEN`. An empty token still
renders as `Authorization: Bearer ` with nothing after it, and the gateway's
HTTP client refuses to send that header and never connects to the addon. An
unset `ADDON_TOKEN` and a `headers:` block that references it is a broken
combination, not a harmless no-op. Read `../../docs/config.md` in `joshua-ai`
for the full `mcp:` shape, including `allow`, `tools`, and per-person
identities. See `../../docs/install.md` for the steps that apply this change,
and for how to add a token.
