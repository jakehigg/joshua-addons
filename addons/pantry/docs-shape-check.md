# Instacart shape check (P2.3)

This is a working note, not a stable page. It checks that the pantry schema
already carries every field a future "add my grocery list to my Instacart
order" flow would need. No Instacart code lands in this addon; this only
checks the shape.

## The walk

One grocery list line, "2 jars of spaghetti sauce", end to end:

1. **List name** — "spaghetti sauce" (or "jarred pasta sauce", a typo, a
   plural). Lives on `Item.name`/`Item.normalized`. Nothing new here.

2. **AKA resolution** — the list name may not match the pantry's own name.
   `resolution.resolve_item` checks the item name, then `ItemAlias`, then a
   conservative fuzzy match. Unchanged by this card; `resolve_product`
   (P2.3) calls it first and raises a clean error when nothing resolves.

3. **Exact product, or candidates and ask** — the household's settled choice
   for this item, if it has one:
   - `upc` → `Product.upc`
   - `description` → `Product.description`
   - `size` / `unit` → `Product.size` / `Product.unit`
   - which store it is normally bought at → `Product.store`

   All four live on the new `Product` row `Item.preferred_product_id`
   points at, and `resolve_product` returns exactly this set, plus
   `confidence`/`source` from `Item`. When no preference is set,
   `preference` is `null` and `candidates` lists every product this item has
   been purchased as, most-recently-purchased first — the ordering flow
   asks the person instead of guessing one.

4. **Quantity** — "2 jars" is on the grocery list itself, not on the pantry.
   The ordering flow carries it through unchanged; pantry has no state to
   add here. `PurchaseRecord.quantity` (P2.2) is available as an optional
   hint ("the household usually buys 2 at a time") if a future flow wants
   one, but nothing is missing for the walk above.

5. **Retailer-specific ids** — an Instacart product id, or any other
   retailer's internal id, is not a pantry concept and should never force a
   schema change when a new retailer shows up. `Product.extra` (JSON,
   nullable) is exactly this: read and written whole, never queried into.

## Verdict

Every field the walk needs already exists after this card's model change.
No new column was added beyond what's listed in the P2.3 checklist.

## One operational gap, noted and left alone on purpose

`Product` rows are created going forward, from `record_purchase` lines that
carry a `upc` (or `sku`+`store`). A purchase recorded before this card
shipped may already have `upc`/`sku` on its `PurchaseRecord` row (P2.2), but
gets no `Product` row until that same product is purchased again.
`resolve_product`'s `candidates` will miss it until then.

A backfill — walk `purchase_records`, find-or-create a `Product` for every
row with a `upc` or `sku`+`store` — was considered and left out of
`run_migrations`. That function runs on every server start; a real-world
database predating this card could have the same `upc` recorded against two
different items (nothing enforced that before `Product` existed), and
`find_or_create_product` refuses that case loudly, by design. Doing that
inside startup migrations would turn one bad historical row into an addon
that will not start. A one-time, offline backfill script is the safer place
for this, run by hand once and reviewed — not automatic. Filing this as a
follow-up rather than adding it here, to keep this card to the schema and
tools it asked for.
