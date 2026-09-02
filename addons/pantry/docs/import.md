# Bulk import

The `import_data` tool loads pantry history in bulk: items, purchases, and
consumption events. Use it for a first-time load from another system, or for
a legacy export. A shopping trip you record live still goes through
`record_purchase`.

## The file format (version 1)

A batch is one JSON object:

```json
{
  "version": 1,
  "items": [ ... ],
  "purchases": [ ... ],
  "consumptions": [ ... ]
}
```

`version` is required and must be `1`. Each section is optional. Send any
combination: items alone, purchases alone, or all three together.

### `items[]`

```json
{
  "name": "Spaghetti Sauce",
  "akas": ["pasta sauce", "marinara"],
  "category": "Pantry",
  "preferred_store": "Aldi",
  "preference": {
    "upc": "111",
    "description": "Rao's Marinara",
    "size": "24 oz",
    "confidence": "auto"
  }
}
```

| Field | Required | Notes |
|---|---|---|
| `name` | yes | The item name. |
| `akas` | no | Alternate names, e.g. receipt names. |
| `category` | no | A category name. Import creates it when missing. |
| `preferred_store` | no | Display hint only. |
| `preference` | no | The exact product this item resolves to. |

A `preference` needs `upc` or `sku`. Its other fields are `description`,
`store`, `size`, `unit`, and `confidence` (`"auto"` or `"low"`, required).

### `purchases[]`

```json
{
  "item": "Spaghetti Sauce",
  "purchased_at": "2026-03-04",
  "store": "Aldi",
  "cost": 4.5,
  "upc": "111",
  "quantity": 1
}
```

`item` and `purchased_at` are required. `purchased_at` takes an ISO date or
an ISO timestamp. `store`, `cost`, `upc`, `sku`, and `quantity` are optional.

### `consumptions[]`

```json
{
  "item": "Spaghetti Sauce",
  "occurred_at": "2026-03-10T18:30:00Z",
  "note": "pasta night"
}
```

`item` and `occurred_at` are required. `occurred_at` takes an ISO timestamp.
`note` is optional.

## What import does

- **Name resolution.** Every `item` name goes through the same lookup
  `record_purchase` uses: the item's own name, then its aliases, then a
  conservative fuzzy match. A name that matches nothing creates a new
  tracked item.
- **Purchases collapse by day.** One item keeps at most one purchase record
  per day, the same rule `record_purchase` follows. A `sku`, `upc`, or
  `quantity` fills a blank field on the existing record. It never
  overwrites a value already on file.
- **A purchase `upc` links a product.** Import finds or creates the
  `Product` row that `upc` (or `sku` and `store`) names, the same way
  `record_purchase` does.
- **A `preference` sets the item's product pin.** Import finds or creates
  the named product and sets it as the item's preferred product, with
  `source` set to `"imported"`. When the item already has a preference set
  by a person (`source` `"manual"`), import does not touch it. It reports
  the difference instead, in the `conflicts` list of the result.
- **Consumptions replay the depletion rule.** Each consumption event feeds
  the same inventory math a live `consume_items` call does, including a
  consumption that pushes an item to `out_of_stock`.
- **Re-import is safe.** Import the same file twice and the second run
  changes nothing. Its result counts show it: zero `created`, the rest
  `merged` or `skipped`.

## Field conflicts

Some fields already hold a value once an item exists: `category`,
`preferred_store`, and a manual product preference. When an import row
names a different value, the import keeps the value on file. It reports
the conflict in the result's `conflicts` list instead of overwriting it
silently. A null field on the item always takes the imported value.

## Sending a large file

`import_data` takes one batch per call. A large dataset arrives as several
calls, each with a slice of `items`, `purchases`, and `consumptions`. 200
rows per section, per call, is a safe chunk size. `scripts/pantry_import.py`
reads a JSON file and drives this chunking for you.

## Validation

`import_data` checks the whole batch before it writes anything. An unknown
field, a missing required field, or a bad value stops the whole call. No
row in the batch applies. The error names the JSON path, for example
`items[3].preference: needs upc or sku`.

An alias that already names a different item is also a validation error. It
names both items so you can fix the row.
