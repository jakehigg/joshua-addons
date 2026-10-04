"""Tests for scripts/chores_import.py: the import driver of the chores addon.

The batching and the summary are pure and tested offline. ``run_import``
drives the real chores app in the same process, over an ASGI transport,
with no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from scripts import chores_import

MEMBERS = [{"slug": "alpha", "name": "Alpha"}, {"slug": "beta", "name": "Beta"}]
CHORE = {
    "external_id": "c-1",
    "member": "alpha",
    "name": "Dishes",
    "points": 10,
    "frequency": "daily",
    "next_due_date": "2026-03-02",
}


def _completion(index: int) -> dict[str, Any]:
    return {
        "external_id": f"done-{index}",
        "external_chore_id": "c-1",
        "completed_at": f"2026-02-{index + 1:02d}T18:00:00Z",
        "points_awarded": 10,
    }


def _transaction(index: int, member: str = "alpha", link: bool = True) -> dict[str, Any]:
    row: dict[str, Any] = {
        "external_id": f"tx-{member}-{index}",
        "member": member,
        "amount": 10,
        "description": "Completed: Dishes",
        "source": "chore",
        "created_at": f"2026-02-{index + 1:02d}T18:00:00Z",
    }
    if link:
        row["completion_external_id"] = f"done-{index}"
    return row


def _first_leaf(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup):
        exc = exc.exceptions[0]
    return exc


# --- offline: batching and summary --------------------------------------------


def test_chunk_rows_splits_into_pieces_of_size_or_less() -> None:
    assert chores_import.chunk_rows(list(range(7)), 3) == [[0, 1, 2], [3, 4, 5], [6]]
    assert chores_import.chunk_rows([], 500) == []


def test_build_calls_sends_members_and_chores_first_then_batches() -> None:
    data = {
        "version": 1,
        "members": MEMBERS,
        "chores": [CHORE],
        "completions": list(range(5)),
        "transactions": list(range(3)),
    }
    calls = chores_import.build_calls(data, 2)
    assert calls == [
        {"version": 1, "members": MEMBERS, "chores": [CHORE]},
        {"version": 1, "members": [], "completions": [0, 1]},
        {"version": 1, "members": [], "completions": [2, 3]},
        {"version": 1, "members": [], "completions": [4]},
        {"version": 1, "members": [], "transactions": [0, 1]},
        {"version": 1, "members": [], "transactions": [2]},
    ]


def test_build_calls_default_batch_is_500_rows() -> None:
    data = {"version": 1, "members": [], "transactions": list(range(1201))}
    calls = chores_import.build_calls(data, chores_import.DEFAULT_BATCH_SIZE)
    assert [len(call.get("transactions", [])) for call in calls] == [0, 500, 500, 201]


def test_build_calls_with_only_members_is_one_call() -> None:
    assert chores_import.build_calls({"version": 1, "members": MEMBERS}, 500) == [
        {"version": 1, "members": MEMBERS, "chores": []}
    ]


def test_merge_result_adds_counts_and_keeps_the_last_balance() -> None:
    totals = chores_import.new_totals()
    chores_import.merge_result(
        totals,
        {
            "counts": {"members": {"created": 2, "skipped": 0}},
            "balances": {"alpha": 0, "beta": 0},
        },
    )
    chores_import.merge_result(
        totals,
        {
            "counts": {"members": {"created": 0, "skipped": 0}},
            "balances": {"alpha": 30},
        },
    )
    assert totals["calls"] == 2
    assert totals["counts"] == {"members": {"created": 2, "skipped": 0}}
    assert totals["balances"] == {"alpha": 30, "beta": 0}


def test_format_summary_lists_counts_and_balances() -> None:
    totals = {
        "calls": 3,
        "counts": {"transactions": {"created": 4, "skipped": 1}},
        "balances": {"alpha": 30},
    }
    text = chores_import.format_summary(totals)
    assert "import summary (3 calls):" in text
    assert "transactions: created=4, skipped=1" in text
    assert "alpha: 30" in text


# --- offline: load_batch -----------------------------------------------------


def test_load_batch_reads_a_valid_file(tmp_path) -> None:
    path = tmp_path / "ok.json"
    path.write_text(json.dumps({"version": 1, "members": MEMBERS}))
    assert chores_import.load_batch(path) == {"version": 1, "members": MEMBERS}


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ([1, 2], "one JSON object"),
        ({"version": 1, "bogus": []}, "bogus"),
        ({"version": 2, "members": []}, "version"),
        ({"version": 1, "members": {"slug": "alpha"}}, "members must be a list"),
    ],
)
def test_load_batch_rejects_a_bad_top_level(tmp_path, content, message) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(content))
    with pytest.raises(ValueError, match=message):
        chores_import.load_batch(path)


# --- offline: main exit codes --------------------------------------------------


def _sample_file(tmp_path) -> Path:
    path = tmp_path / "sample.json"
    path.write_text(json.dumps({"version": 1, "members": MEMBERS}))
    return path


def test_main_prints_counts_and_balances(tmp_path, monkeypatch, capsys) -> None:
    async def _fake(*args, **kwargs):
        return {
            "calls": 1,
            "counts": {"members": {"created": 2, "skipped": 0}},
            "balances": {"alpha": 0, "beta": 0},
        }

    monkeypatch.setattr(chores_import, "run_import", _fake)
    assert chores_import.main([str(_sample_file(tmp_path))]) == 0
    out = capsys.readouterr().out
    assert "members: created=2, skipped=0" in out
    assert "beta: 0" in out


def test_main_exits_non_zero_on_a_tool_error(tmp_path, monkeypatch, capsys) -> None:
    async def _fake(*args, **kwargs):
        raise RuntimeError("import rejected:\n- members[0].slug: bad")

    monkeypatch.setattr(chores_import, "run_import", _fake)
    assert chores_import.main([str(_sample_file(tmp_path))]) == 1
    assert "import rejected" in capsys.readouterr().err


def test_main_names_the_error_inside_an_exception_group(tmp_path, monkeypatch, capsys) -> None:
    async def _fake(*args, **kwargs):
        raise ExceptionGroup("unhandled errors", [ValueError("401 Unauthorized")])

    monkeypatch.setattr(chores_import, "run_import", _fake)
    assert chores_import.main([str(_sample_file(tmp_path))]) == 1
    assert "401 Unauthorized" in capsys.readouterr().err


def test_main_exits_non_zero_on_a_bad_file(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"version": 2}))
    assert chores_import.main([str(path)]) == 1


def test_main_rejects_a_batch_size_below_one(tmp_path, capsys) -> None:
    assert chores_import.main([str(_sample_file(tmp_path)), "--batch-size", "0"]) == 1
    assert "batch-size" in capsys.readouterr().err


# --- in process: the real chores app over the real MCP transport -------------


def _build_app(monkeypatch, tmp_path, token: str | None = None):
    if token is None:
        monkeypatch.delenv("ADDON_TOKEN", raising=False)
    else:
        monkeypatch.setenv("ADDON_TOKEN", token)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("CHORES_DB", str(tmp_path / "chores.db"))

    from joshua_chores.server import build_app

    return build_app()


async def _drive(app, data, batch_size, token=None, http_client=None, runs=1):
    """Run the import ``runs`` times in one run of the server session manager."""
    from joshua_chores.server import mcp as chores_mcp

    results = []
    async with chores_mcp.session_manager.run():
        for _ in range(runs):
            results.append(
                await chores_import.run_import(
                    data, "http://testserver/mcp", token, batch_size, http_client=http_client
                )
            )
    return results[0] if runs == 1 else results


def _client(app) -> httpx2.AsyncClient:
    transport = httpx2.ASGITransport(app=app)
    return httpx2.AsyncClient(transport=transport, base_url="http://testserver", timeout=10)


async def test_run_import_sends_batches_to_the_real_app(monkeypatch, tmp_path) -> None:
    app = _build_app(monkeypatch, tmp_path)
    data = {
        "version": 1,
        "members": MEMBERS,
        "chores": [CHORE],
        "completions": [_completion(i) for i in range(5)],
        "transactions": [_transaction(i) for i in range(5)]
        + [_transaction(0, member="beta", link=False)],
    }

    client = _client(app)
    try:
        totals, again = await _drive(app, data, 2, http_client=client, runs=2)
    finally:
        await client.aclose()

    # 1 call for members and chores, 3 for 5 completions, 3 for 6 ledger rows.
    assert totals["calls"] == 7
    assert totals["counts"] == {
        "members": {"created": 2, "skipped": 0},
        "chores": {"created": 1, "skipped": 0},
        "completions": {"created": 5, "skipped": 0},
        "transactions": {"created": 6, "skipped": 0},
    }
    assert totals["balances"] == {"alpha": 50, "beta": 10}
    assert again["counts"]["transactions"] == {"created": 0, "skipped": 6}
    assert again["balances"] == {"alpha": 50, "beta": 10}


async def test_run_import_raises_on_a_rejected_batch(monkeypatch, tmp_path) -> None:
    """The server session manager wraps the error in an ``ExceptionGroup`` here.

    A real CLI run has no such wrapper. ``subgroup`` finds the RuntimeError.
    """
    app = _build_app(monkeypatch, tmp_path)
    data = {"version": 1, "members": [{"slug": "alpha", "name": "Alpha", "bogus": True}]}

    client = _client(app)
    try:
        with pytest.raises(BaseExceptionGroup) as excinfo:
            await _drive(app, data, 500, http_client=client)
        matched = excinfo.value.subgroup(RuntimeError)
        assert matched is not None
        assert "bogus" in str(_first_leaf(matched))
    finally:
        await client.aclose()


async def test_run_import_sends_the_token_as_a_bearer_header(monkeypatch, tmp_path) -> None:
    app = _build_app(monkeypatch, tmp_path, token="secret")
    transport = httpx2.ASGITransport(app=app)
    captured: dict[str, Any] = {}

    class _CapturingClient(httpx2.AsyncClient):
        def __init__(self, *args, **kwargs):
            captured["headers"] = kwargs.get("headers")
            kwargs["transport"] = transport
            kwargs["base_url"] = "http://testserver"
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(chores_import.httpx2, "AsyncClient", _CapturingClient)
    totals = await _drive(app, {"version": 1, "members": MEMBERS}, 500, token="secret")

    assert captured["headers"] == {"Authorization": "Bearer secret"}
    assert totals["counts"]["members"] == {"created": 2, "skipped": 0}
