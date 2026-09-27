"""The task store: SQLite at ``<DEVELOPER_DATA_DIR>/developer.db``.

Two tables, created on first open. A column added after the first release
is added to an old database when it opens. ``tasks`` holds one row for each
``develop`` or ``rework`` call; ``history`` holds the reports of the
earlier workers of a resumed task. ``settings`` holds the values a person changed
from chat. One connection serves the process, behind a lock, so a call from
any thread is safe.
"""

from __future__ import annotations

import hmac
import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TASK_TYPES = ("develop", "rework")
STATUSES = ("dispatched", "running", "success", "failed", "blocked", "timed_out")
ACTIVE_STATUSES = ("dispatched", "running")
TERMINAL_STATUSES = ("success", "failed", "blocked", "timed_out")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    person TEXT NOT NULL,
    task_type TEXT NOT NULL,
    repo TEXT NOT NULL,
    scope TEXT NOT NULL,
    branch_name TEXT,
    base_branch TEXT,
    persona TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'dispatched',
    brief TEXT NOT NULL DEFAULT '',
    instructions TEXT,
    pr_url TEXT,
    pr_number INTEGER,
    commit_hash TEXT,
    summary TEXT,
    report TEXT,
    error TEXT,
    open_question TEXT,
    answer TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    estimated_cost REAL,
    notify TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    worker_token TEXT,
    session_log TEXT,
    scan TEXT,
    findings TEXT,
    history TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_person_created ON tasks(person, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS settings (
    person TEXT PRIMARY KEY,
    git_name TEXT,
    git_email TEXT,
    default_persona TEXT,
    notify TEXT,
    updated_at TEXT NOT NULL
);
"""

# The columns task_status and list_tasks return. The brief, the feedback, the
# report, and the answer stay out, to keep a status small.
STATUS_COLUMNS = (
    "task_id",
    "person",
    "task_type",
    "repo",
    "scope",
    "branch_name",
    "base_branch",
    "persona",
    "status",
    "pr_url",
    "pr_number",
    "commit_hash",
    "summary",
    "error",
    "open_question",
    "input_tokens",
    "output_tokens",
    "estimated_cost",
    "notify",
    "scan",
    "findings",
    "created_at",
    "started_at",
    "completed_at",
)

# The fields update_task may change.
ALLOWED_UPDATE_FIELDS = frozenset(
    {
        "status",
        "branch_name",
        "base_branch",
        "pr_url",
        "pr_number",
        "commit_hash",
        "summary",
        "report",
        "error",
        "open_question",
        "answer",
        "input_tokens",
        "output_tokens",
        "estimated_cost",
        "started_at",
        "completed_at",
        "scan",
        "findings",
        "worker_token",
        "history",
    }
)

SETTINGS_FIELDS = ("git_name", "git_email", "default_persona", "notify")

# Columns an older database does not have: name to type.
ADDED_COLUMNS = {
    "worker_token": "TEXT",
    "session_log": "TEXT",
    "scan": "TEXT",
    "findings": "TEXT",
    "history": "TEXT",
}

# The columns that hold JSON.
JSON_COLUMNS = ("report", "findings", "history")

# The session log stops at this size. More text is dropped.
MAX_SESSION_LOG_BYTES = 1024 * 1024


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def build_update_sql(task_id: str, fields: dict[str, Any]) -> tuple[str, list[Any]]:
    """Build a parameterized UPDATE from ``fields``.

    Raises ValueError for an unknown field, a bad status, or no fields.
    """
    unknown = set(fields) - ALLOWED_UPDATE_FIELDS
    if unknown:
        raise ValueError(f"unknown update fields: {sorted(unknown)}")
    if not fields:
        raise ValueError("no fields to update")
    if "status" in fields and fields["status"] not in STATUSES:
        raise ValueError(f"unknown status: {fields['status']!r}")
    clauses: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        clauses.append(f"{key} = ?")
        if key in JSON_COLUMNS and value is not None and not isinstance(value, str):
            value = json.dumps(value)
        params.append(value)
    params.append(task_id)
    return f"UPDATE tasks SET {', '.join(clauses)} WHERE task_id = ?", params


def _row(row: sqlite3.Row | None, columns: Iterable[str] | None = None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    if columns is not None:
        data = {key: data[key] for key in columns}
    for key in JSON_COLUMNS:
        if data.get(key):
            data[key] = json.loads(data[key])
    return data


class TaskStore:
    """The SQLite store for tasks and person settings."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            have = {row[1] for row in self._conn.execute("PRAGMA table_info(tasks)")}
            for name, kind in ADDED_COLUMNS.items():
                if name not in have:
                    self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {kind}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    # --- tasks -------------------------------------------------------------

    def create_task(
        self,
        *,
        task_id: str,
        person: str,
        task_type: str,
        repo: str,
        scope: str,
        persona: str,
        brief: str = "",
        instructions: str | None = None,
        branch_name: str | None = None,
        base_branch: str | None = None,
        pr_number: int | None = None,
        pr_url: str | None = None,
        notify: str | None = None,
        worker_token: str | None = None,
    ) -> None:
        if task_type not in TASK_TYPES:
            raise ValueError(f"unknown task type: {task_type!r}")
        self._execute(
            """
            INSERT INTO tasks (
                task_id, person, task_type, repo, scope, persona, status, brief,
                instructions, branch_name, base_branch, pr_number, pr_url, notify, created_at,
                worker_token
            ) VALUES (?, ?, ?, ?, ?, ?, 'dispatched', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                person,
                task_type,
                repo,
                scope,
                persona,
                brief,
                instructions,
                branch_name,
                base_branch,
                pr_number,
                pr_url,
                notify,
                now_iso(),
                worker_token,
            ),
        )

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """The status fields of one task."""
        cursor = self._execute(
            f"SELECT {', '.join(STATUS_COLUMNS)} FROM tasks WHERE task_id = ?", (task_id,)
        )
        return _row(cursor.fetchone())

    def get_task_full(self, task_id: str) -> dict[str, Any] | None:
        """Every field of one task, with the report parsed."""
        cursor = self._execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        return _row(cursor.fetchone())

    def update_task(self, task_id: str, **fields: Any) -> None:
        sql, params = build_update_sql(task_id, fields)
        self._execute(sql, params)

    def list_tasks(self, person: str | None, limit: int = 10) -> list[dict[str, Any]]:
        """The newest tasks first. ``person`` None lists the tasks of every person."""
        columns = ", ".join(STATUS_COLUMNS)
        if person is None:
            cursor = self._execute(
                f"SELECT {columns} FROM tasks ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            )
        else:
            cursor = self._execute(
                f"SELECT {columns} FROM tasks WHERE person = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (person, limit),
            )
        return [row for row in (_row(r) for r in cursor.fetchall()) if row is not None]

    def list_active_tasks(self) -> list[dict[str, Any]]:
        """The tasks in ``dispatched`` or ``running``, oldest first."""
        cursor = self._execute(
            f"SELECT {', '.join(STATUS_COLUMNS)} FROM tasks "
            "WHERE status IN ('dispatched', 'running') ORDER BY created_at, rowid"
        )
        return [row for row in (_row(r) for r in cursor.fetchall()) if row is not None]

    def fail_active_tasks(self, error: str) -> list[dict[str, Any]]:
        """Mark every active task ``failed`` with ``error``. Returns the tasks it changed."""
        active = self.list_active_tasks()
        stamp = now_iso()
        for task in active:
            self.update_task(task["task_id"], status="failed", error=error, completed_at=stamp)
        return active

    # --- worker tokens and the session log ---------------------------------

    def find_task_by_worker_token(self, token: str) -> str | None:
        """The id of the task that holds ``token``, or None.

        Every stored token is compared with ``hmac.compare_digest``, and the
        loop does not stop at a match.
        """
        if not token:
            return None
        cursor = self._execute(
            "SELECT task_id, worker_token FROM tasks WHERE worker_token IS NOT NULL"
        )
        found = None
        provided = token.encode()
        for task_id, stored in cursor.fetchall():
            if hmac.compare_digest(provided, stored.encode()):
                found = task_id
        return found

    def clear_ended_worker_tokens(self) -> int:
        """Remove the worker token of every task that ended. Returns the count."""
        cursor = self._execute(
            "UPDATE tasks SET worker_token = NULL WHERE worker_token IS NOT NULL "
            "AND status NOT IN ('dispatched', 'running')"
        )
        return cursor.rowcount

    def append_session_log(self, task_id: str, text: str) -> bool:
        """Add ``text`` to the session log of a task.

        Returns False, and adds nothing, when the log would pass
        ``MAX_SESSION_LOG_BYTES``.
        """
        size = len(text.encode("utf-8"))
        cursor = self._execute(
            "UPDATE tasks SET session_log = COALESCE(session_log, '') || ? "
            "WHERE task_id = ? AND length(CAST(COALESCE(session_log, '') AS BLOB)) + ? <= ?",
            (text, task_id, size, MAX_SESSION_LOG_BYTES),
        )
        return cursor.rowcount == 1

    # --- settings ----------------------------------------------------------

    def get_settings(self, person: str) -> dict[str, Any]:
        """The values ``person`` changed from chat. A field never set is None."""
        cursor = self._execute("SELECT * FROM settings WHERE person = ?", (person,))
        row = cursor.fetchone()
        if row is None:
            return dict.fromkeys(SETTINGS_FIELDS)
        return {key: row[key] for key in SETTINGS_FIELDS}

    def set_settings(self, person: str, **fields: str | None) -> dict[str, Any]:
        """Change the given fields. None leaves a field as it is; "" clears it."""
        unknown = set(fields) - set(SETTINGS_FIELDS)
        if unknown:
            raise ValueError(f"unknown settings fields: {sorted(unknown)}")
        current = self.get_settings(person)
        for key, value in fields.items():
            if value is not None:
                current[key] = value or None
        self._execute(
            """
            INSERT INTO settings (person, git_name, git_email, default_persona, notify, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(person) DO UPDATE SET
                git_name = excluded.git_name,
                git_email = excluded.git_email,
                default_persona = excluded.default_persona,
                notify = excluded.notify,
                updated_at = excluded.updated_at
            """,
            (person, *(current[key] for key in SETTINGS_FIELDS), now_iso()),
        )
        return current
