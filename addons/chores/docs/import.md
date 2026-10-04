# Bulk import

The `import_data` tool loads chores data in bulk: members, chores,
completions, and ledger rows. Use it to move a household from another
system. Each row keeps its original timestamps, so the ledger history stays
complete and each balance is the same after the import.

## The format (version 1)

A batch is one JSON object:

```json
{
  "version": 1,
  "members": [ ... ],
  "chores": [ ... ],
  "completions": [ ... ],
  "transactions": [ ... ]
}
```

`version` is necessary and must be `1`. `members` is necessary, and it can
be empty. The other sections are optional.

A timestamp is an ISO 8601 value, for example `2026-02-01T18:00:00Z`. A
timestamp with an offset changes to UTC. A timestamp without an offset is
UTC. A date is `YYYY-MM-DD`.

### `members[]`

| Field | Necessary | Notes |
|---|---|---|
| `slug` | yes | 1 to 32 lowercase letters, digits, or hyphens. |
| `name` | yes | The name to show. |
| `is_active` | no | Default `true`. |
| `sort_order` | no | Default `0`. |

### `chores[]`

| Field | Necessary | Notes |
|---|---|---|
| `member` | yes | The slug of a member in this batch or in the database. |
| `name` | yes | The name of the chore. |
| `points` | yes | A whole number more than 0. |
| `frequency` | yes | `daily`, `weekly`, `monthly`, or `one_off`. |
| `next_due_date` | yes | A date. |
| `is_active` | no | Default `true`. |
| `last_completed_at` | no | A timestamp. |
| `created_at` | no | A timestamp. Default: the time of the import. |
| `external_id` | no | The id of the chore in the export. A completion can refer to it. |

### `completions[]`

| Field | Necessary | Notes |
|---|---|---|
| `external_id` | yes | The id of the completion in the export. |
| `external_chore_id` | one of two | The `external_id` of the chore. |
| `member` and `chore` | one of two | The slug of the member and the name of the chore. The member must have only one chore with that name. |
| `completed_at` | yes | A timestamp. |
| `points_awarded` | yes | A whole number. |
| `note` | no | Text. |

Give `external_chore_id`, or `member` and `chore`. Do not give both. A
completion does not write a ledger row. Put the XP of the completion in
`transactions`.

### `transactions[]`

| Field | Necessary | Notes |
|---|---|---|
| `external_id` | yes | The id of the ledger row in the export. |
| `member` | yes | A slug. |
| `amount` | yes | A whole number. Positive for XP in, negative for XP out. |
| `description` | yes | Text. |
| `source` | yes | `chore`, `one_off`, `withdrawal`, or `adjustment`. |
| `created_at` | yes | A timestamp. |
| `completion_external_id` | no | The `external_id` of the completion that gave this XP. |

The balance of a member is the sum of the `amount` values of that member.

## Example

```json
{
  "version": 1,
  "members": [{"slug": "alpha", "name": "Alpha"}],
  "chores": [
    {
      "external_id": "chore-1",
      "member": "alpha",
      "name": "Dishes",
      "points": 10,
      "frequency": "daily",
      "next_due_date": "2026-02-03",
      "created_at": "2026-01-01T09:00:00Z"
    }
  ],
  "completions": [
    {
      "external_id": "completion-1",
      "external_chore_id": "chore-1",
      "completed_at": "2026-02-02T18:00:00Z",
      "points_awarded": 10
    }
  ],
  "transactions": [
    {
      "external_id": "tx-1",
      "member": "alpha",
      "amount": 10,
      "description": "Completed: Dishes",
      "source": "chore",
      "created_at": "2026-02-02T18:00:00Z",
      "completion_external_id": "completion-1"
    }
  ]
}
```

The result gives the counts and the balance of each member in the batch:

```json
{
  "version": 1,
  "counts": {
    "members": {"created": 1, "skipped": 0},
    "chores": {"created": 1, "skipped": 0},
    "completions": {"created": 1, "skipped": 0},
    "transactions": {"created": 1, "skipped": 0}
  },
  "balances": {"alpha": 10}
}
```

## A second import

You can import the same batch again. The second import changes nothing,
and each count is `skipped`. The import finds an existing row as follows:

- A member: the same `slug`.
- A chore: the same `external_id`, or the same member, name, and
  `created_at`. When the row has no `created_at`, the same member and name.
- A completion or a ledger row: the same `external_id`.

The import does not change a row that exists. To correct a row, use the
other tools.

## Checks

The import checks the full batch before it writes. An unknown field, a
missing field, a bad value, an `external_id` that is in the batch two
times, or a reference to a row that does not exist stops the call. The
import then writes nothing. The error gives the path of each problem, for
example `chores[2].points: Input should be greater than 0`.

## Export from the old chores app

The script `scripts/export_legacy_chores.py` reads the Postgres database of
the old chores app and writes a version 1 file. The script only reads. Each
query is a SELECT, and the script never commits. The script
`scripts/chores_import.py` then sends the file to `import_data`.

### Procedure

1. Open a port-forward to the old database, in a first terminal:

   ```sh
   kubectl -n <namespace> port-forward svc/<service> 5432:5432
   ```

2. Run the export from the repository root, in a second terminal:

   ```sh
   DATABASE_URL=postgresql://<user>:<password>@localhost:5432/<db> \
     uv run --with asyncpg python scripts/export_legacy_chores.py --out chores_export.json
   ```

3. Read the summary on stderr. It gives the row count of each table and
   the stored balance of each member.

4. If the export stops with exit code 2, read the integrity gate below.

5. Run the import:

   ```sh
   uv run python scripts/chores_import.py chores_export.json \
     --url https://<chores-addon>/mcp --token <ADDON_TOKEN>
   ```

6. Compare the balances that the import prints with the stored balances
   from step 3. They must be equal.

The import script sends the members and the chores in the first call.
Then it sends the completions and the ledger rows in batches of 500
rows. Use `--batch-size` to change the batch size. If a call fails, the
script exits with code 1. A second run of the same file changes nothing,
so you can run the same command again.

### The integrity gate

The old app keeps a stored balance for each member. The addon calculates
the balance from the ledger. Before it writes the file, the export
compares the stored balance with the sum of the ledger of each member.

If a member fails the check, the export lists each mismatch and exits
with code 2. It does not write the file. Correct the old data, then run
the export again.

With `--force`, the export writes the file and prints a warning. The
import then gives each member the sum of the ledger, not the stored
balance.

A value that the import rejects also stops the export, with exit code 1.
Examples are a slug that is not valid and a chore with 0 points.
`--force` does not change this.

### What the export maps

| Old table | Section | `external_id` |
|---|---|---|
| `kids` | `members` | none, the `slug` is the key |
| `chores` | `chores` | `legacy-chore-<id>` |
| `completions` | `completions` | `legacy-completion-<id>` |
| `transactions` | `transactions` | `legacy-tx-<id>` |

The order of the kid ids gives `sort_order`. Each member is active. A
completion refers to its chore with `external_chore_id`, and the member
of the chore is the member of the completion. A ledger row with source
`chore` gets `completion_external_id` when its `reference_id` is an
exported completion.

The file does not keep these old values:

- The stored balance of a member. The ledger gives the balance.
- The `updated_at` time of a chore. The import sets it to `created_at`.
- The kid of a completion. The summary counts each completion with a kid
  that is not the kid of its chore.
- A `reference_id` that is not a link from a `chore` row to an exported
  completion.
