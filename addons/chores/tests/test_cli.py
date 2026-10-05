"""``python -m joshua_chores``: the ``serve`` and ``import`` commands.

Each import test runs the real command against a new SQLite file, and then
reads the file with ``sqlite3``.
"""

from __future__ import annotations

import io
import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from joshua_chores import cli

FIXTURE = Path(__file__).parent / "fixtures" / "import_v1.json"
SETTINGS = ("XP_PER_DOLLAR", "COOLDOWN_SECONDS", "MANAGER_LABEL", "CHORES_TZ", "MANAGER_PIN")
TABLES = ("members", "chores", "completions", "transactions")
BALANCES = {"alpha": -15, "beta": 45, "gamma": 0}
CREATED = {
    "members": {"created": 3, "skipped": 0},
    "chores": {"created": 3, "skipped": 0},
    "completions": {"created": 3, "skipped": 0},
    "transactions": {"created": 5, "skipped": 0},
}
SKIPPED = {
    "members": {"created": 0, "skipped": 3},
    "chores": {"created": 0, "skipped": 3},
    "completions": {"created": 0, "skipped": 3},
    "transactions": {"created": 0, "skipped": 5},
}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path) -> Path:
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    db_path = tmp_path / "chores.db"
    monkeypatch.setenv("CHORES_DB", str(db_path))
    return db_path


@pytest.fixture(autouse=True)
def _restore_logging():
    """Put back the root handlers, so that no handler keeps a closed capture stream."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def db_path(_env: Path) -> Path:
    return _env


def _write(tmp_path: Path, document: Any, name: str = "chores_export.json") -> Path:
    path = tmp_path / name
    path.write_text(document if isinstance(document, str) else json.dumps(document))
    return path


def _row_counts(db_path: Path) -> dict[str, int]:
    with closing(sqlite3.connect(db_path)) as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES
        }


def _document() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def test_no_command_and_serve_run_the_server(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.append(kwargs))
    assert cli.main([]) == 0
    assert cli.main(["serve"]) == 0
    assert calls == [{"host": "0.0.0.0", "port": 8000, "log_config": None}] * 2


def test_an_unknown_command_exits_2(capsys) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main(["export"])
    assert caught.value.code == 2


def test_import_from_a_file(tmp_path, db_path, capsys) -> None:
    path = _write(tmp_path, _document())
    assert cli.main(["import", str(path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"version": 1, "counts": CREATED, "balances": BALANCES}
    assert _row_counts(db_path) == {
        "members": 3,
        "chores": 3,
        "completions": 3,
        "transactions": 5,
    }


def test_import_from_stdin(monkeypatch, db_path, capsys) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(FIXTURE.read_text()))
    assert cli.main(["import", "-"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["counts"] == CREATED
    assert out["balances"] == BALANCES


def test_a_second_run_skips_everything(tmp_path, db_path, capsys) -> None:
    path = _write(tmp_path, _document())
    assert cli.main(["import", str(path)]) == 0
    capsys.readouterr()
    before = _row_counts(db_path)
    assert cli.main(["import", str(path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"version": 1, "counts": SKIPPED, "balances": BALANCES}
    assert _row_counts(db_path) == before


def test_an_invalid_row_exits_1_and_writes_nothing(tmp_path, db_path, capsys) -> None:
    document = _document()
    document["chores"][0]["points"] = 0
    document["transactions"][0]["source"] = "gift"
    path = _write(tmp_path, document)

    assert cli.main(["import", str(path)]) == 1
    captured = capsys.readouterr()
    lines = captured.err.splitlines()
    assert captured.out == ""
    assert lines[0] == "import rejected:"
    assert any(line.startswith("- chores[0].points:") for line in lines)
    assert any(line.startswith("- transactions[0]:") for line in lines)
    assert _row_counts(db_path) == {table: 0 for table in TABLES}


def test_an_error_during_the_write_rolls_back_every_row(tmp_path, db_path, capsys) -> None:
    """The check passes, but the write finds two chores with one name. Nothing stays."""
    chores = [
        {
            "member": "alpha",
            "name": "Dishes",
            "points": 1,
            "frequency": "daily",
            "next_due_date": "2026-01-01",
            "created_at": created,
        }
        for created in ("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
    ]
    completion = {
        "external_id": "c-1",
        "member": "alpha",
        "chore": "Dishes",
        "completed_at": "2026-01-03T00:00:00Z",
        "points_awarded": 1,
    }
    document = {
        "version": 1,
        "members": [{"slug": "alpha", "name": "Alpha"}],
        "chores": chores,
        "completions": [completion],
    }
    assert cli.main(["import", str(_write(tmp_path, document))]) == 1
    assert "more than one chore" in capsys.readouterr().err
    assert _row_counts(db_path) == {table: 0 for table in TABLES}


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('{"version": 1, "members": [', "the file is not JSON"),
        ("[]", "one JSON object"),
        ({"version": 2, "members": []}, "version must be 1"),
        ({"version": True, "members": []}, "version must be 1"),
        ({"members": []}, "version must be 1"),
        ({"version": 1}, "members is necessary"),
        ({"version": 1, "members": [], "pets": []}, "unknown top-level field 'pets'"),
        ({"version": 1, "members": {}}, "members must be a list"),
    ],
)
def test_a_bad_file_exits_2_and_writes_nothing(
    tmp_path, db_path, capsys, content: Any, message: str
) -> None:
    path = _write(tmp_path, content)
    assert cli.main(["import", str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    assert not db_path.exists()


def test_a_missing_file_exits_2(tmp_path, db_path, capsys) -> None:
    assert cli.main(["import", str(tmp_path / "nothing.json")]) == 2
    assert "cannot read" in capsys.readouterr().err
    assert not db_path.exists()


def test_the_error_never_shows_the_file_content(tmp_path, capsys) -> None:
    path = _write(tmp_path, '{"version": 1, "members": [{"name": "secret-value"}')
    assert cli.main(["import", str(path)]) == 2
    captured = capsys.readouterr()
    assert "secret-value" not in captured.err
    assert "secret-value" not in captured.out


def test_a_bad_setting_exits_1_and_writes_nothing(tmp_path, db_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("CHORES_TZ", "Not/AZone")
    assert cli.main(["import", str(_write(tmp_path, _document()))]) == 1
    assert "CHORES_TZ" in capsys.readouterr().err
    assert not db_path.exists()


def test_the_import_logs_to_stderr_not_stdout(tmp_path, capsys) -> None:
    path = _write(tmp_path, _document())
    assert cli.main(["import", str(path)]) == 0
    logging.getLogger("chores.test").warning({"message": "a log line"})
    captured = capsys.readouterr()
    json.loads(captured.out)
    assert "a log line" in captured.err
