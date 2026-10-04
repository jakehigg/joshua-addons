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
