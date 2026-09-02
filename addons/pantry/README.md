# pantry

The `pantry` addon tracks a household's grocery purchases and turns them into
a virtual pantry: what is probably in stock, and when it is likely to run out,
based on how often each item gets bought.

## The receipt workflow

The main tool is `record_purchase`. After a shopping trip, read every line on
the receipt and send them all in one call, with the date printed on the
receipt. Include the SKU and UPC digits when the receipt prints them. Ask the
person about a line you cannot read, instead of guessing it.

A name on the receipt that matches a tracked item, or one of its aliases,
records onto that item. A name that matches nothing creates a new tracked
item, so a receipt name never has to match the pantry exactly first. Recording
the same item twice for the same date merges into one purchase; a later call
can still fill in a SKU or UPC an earlier call left blank.

## Preferred products

An item like "spaghetti sauce" can cover more than one product — a jar of
Rao's, a jar of the store brand. A receipt line with a UPC, or a SKU and a
store, finds or creates that exact product on the item. `resolve_product`
looks up an item's preference: a pin, when `set_preferred_product` has set
one, or `candidates` — the products seen on past receipts — when it has not.
No preference is a real answer, not a gap: it means ask the person, never
guess from the candidates.

## Bulk import

`import_data` loads a household's pantry history in one pass: items, with
their aliases and preferences, purchases, and consumption events. Read
`docs/import.md` for the file format and `scripts/pantry_import.py` for a
command-line driver.

## Tools

| Tool | What it does |
|---|---|
| `record_purchase` | Record a receipt: every item bought, on one purchase date. |
| `get_inventory` | List every tracked item's status, with an optional filter. |
| `get_item_history` | Purchase history and frequency data for one item. |
| `get_item_cost` | Average and most recent cost for one or more items. |
| `get_price_stats` | Cost stats for one item: overall, per store, per product. |
| `consume_items` | Mark items consumed, e.g. after a meal. |
| `set_preferred_store` | Set or clear the store an item is usually bought at. |
| `resolve_product` | Look up the exact product an item resolves to, or its candidates. |
| `set_preferred_product` | Pin, or clear, the exact product an item resolves to. |
| `set_purchase_cost` | Attach a price to a purchase that already exists. |
| `set_purchase_store` | Tag which store one or more existing purchases came from. |
| `delete_purchase` | Delete a single purchase record, keeping the item. |
| `delete_item` | Permanently delete a tracked item and all its history. |
| `add_alias` | Teach the pantry that one name refers to an existing item. |
| `list_aliases` | List the alternate names on file for one item, or every item. |
| `import_data` | Load pantry history in bulk: items, purchases, and consumption events. See `docs/import.md`. |

## Run it

The addon serves MCP at `POST /mcp` (streamable HTTP) on port 8000, and answers
`GET /healthz` with `{"ok": true}`.

## Settings

| Variable | Default | What it does |
|---|---|---|
| `ADDON_TOKEN` | not set | When set, every request to `/mcp` needs the header `Authorization: Bearer <ADDON_TOKEN>`. A missing or wrong token gets 401. `/healthz` stays open. When `ADDON_TOKEN` is not set, the addon checks no token; the docker network is the boundary. |
| `DATABASE_URL` | not set | A full Postgres URL. When set, the addon uses Postgres instead of the bundled SQLite file. |
| `PANTRY_DB` | `/data/pantry.db` | Where the SQLite database file lives, when `DATABASE_URL` is not set. |
| `LOG_LEVEL` | `INFO` | The log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |

## Add it to joshua.yaml

Add this block to the `mcp:` section of your `joshua.yaml`:

```yaml
mcp:
  pantry:
    type: http
    url: http://pantry:8000/mcp
    allow: all
```

Set no `headers:` block when `ADDON_TOKEN` is not set. An empty
`Authorization` header there sends `Bearer ` on every request, and this addon
rejects that as a wrong token once you do set `ADDON_TOKEN`. Leave the whole
`headers:` block out until you have a real token to put in it.

Read `../../docs/config.md` in `joshua-ai` for the full `mcp:` shape, including
`allow`, `tools`, and per-person identities. See `../../docs/install.md` for
the steps that apply this change, and for how to add a token.
