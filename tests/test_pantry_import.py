"""Tests for scripts/pantry_import.py: the bulk-import CLI driver.

Chunking and summary logic are pure and tested offline. The HTTP driving
(``run_import``) is proven against the real pantry addon app, in-process
over an ASGI transport — no network, no live instance.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from scripts import pantry_import

ROOT = Path(__file__).resolve().parent.parent


def _first_leaf(exc: BaseException) -> BaseException:
    """Walk an (possibly nested) ExceptionGroup down to its first real leaf."""
    while isinstance(exc, BaseExceptionGroup):
        exc = exc.exceptions[0]
    return exc


# --- offline: chunking and summary ----------------------------------------


def test_chunk_rows_splits_into_pieces_of_at_most_size() -> None:
    rows = list(range(7))
    assert pantry_import.chunk_rows(rows, 3) == [[0, 1, 2], [3, 4, 5], [6]]


def test_chunk_rows_of_empty_list_is_empty() -> None:
    assert pantry_import.chunk_rows([], 200) == []


def test_build_calls_chunks_each_section_separately() -> None:
    data = {
        "version": 1,
        "items": list(range(3)),
        "purchases": list(range(5)),
    }
    calls = pantry_import.build_calls(data, 2)
    assert calls == [
        {"version": 1, "items": [0, 1]},
        {"version": 1, "items": [2]},
        {"version": 1, "purchases": [0, 1]},
        {"version": 1, "purchases": [2, 3]},
        {"version": 1, "purchases": [4]},
    ]


def test_build_calls_skips_an_empty_or_missing_section() -> None:
    calls = pantry_import.build_calls({"version": 1, "items": []}, 200)
    assert calls == []


def test_merge_summary_adds_counts_and_collects_conflicts() -> None:
    totals = pantry_import.new_totals()
    pantry_import.merge_summary(
        totals,
        {
            "counts": {"items": {"created": 2, "merged": 0, "skipped": 0}},
            "conflicts": [{"type": "item_category", "item": "Milk"}],
        },
    )
    pantry_import.merge_summary(
        totals,
        {
            "counts": {"items": {"created": 1, "merged": 1, "skipped": 0}},
            "conflicts": [],
        },
    )
    assert totals["counts"] == {"items": {"created": 3, "merged": 1, "skipped": 0}}
    assert totals["conflicts"] == [{"type": "item_category", "item": "Milk"}]


def test_format_summary_lists_every_section_and_conflict_count() -> None:
    totals = {
        "counts": {"items": {"created": 1, "merged": 0, "skipped": 0}},
        "conflicts": [{"type": "item_category", "item": "Milk"}],
    }
    text = pantry_import.format_summary(totals)
    assert "items: created=1, merged=0, skipped=0" in text
    assert "conflicts: 1" in text


# --- offline: load_batch's top-level checks --------------------------------


def test_load_batch_reads_a_valid_file(tmp_path) -> None:
    path = tmp_path / "sample.json"
    path.write_text(json.dumps({"version": 1, "items": [{"name": "Milk"}]}))
    assert pantry_import.load_batch(path) == {"version": 1, "items": [{"name": "Milk"}]}


def test_load_batch_rejects_an_unknown_top_level_field(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"version": 1, "bogus": []}))
    with pytest.raises(ValueError, match="bogus"):
        pantry_import.load_batch(path)


def test_load_batch_rejects_an_unsupported_version(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"version": 2}))
    with pytest.raises(ValueError, match="version"):
        pantry_import.load_batch(path)


def test_load_batch_rejects_a_section_that_is_not_a_list(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"version": 1, "items": {"name": "Milk"}}))
    with pytest.raises(ValueError, match="items"):
        pantry_import.load_batch(path)


# --- offline: main()'s exit codes, run_import mocked out -------------------


def _sample_file(tmp_path) -> Path:
    path = tmp_path / "sample.json"
    path.write_text(json.dumps({"version": 1, "items": [{"name": "Milk"}]}))
    return path


def test_main_exits_zero_on_a_clean_import(tmp_path, monkeypatch, capsys) -> None:
    async def _fake_run_import(*args, **kwargs):
        return {"counts": {"items": {"created": 1, "merged": 0, "skipped": 0}}, "conflicts": []}

    monkeypatch.setattr(pantry_import, "run_import", _fake_run_import)
    code = pantry_import.main([str(_sample_file(tmp_path))])
    assert code == 0
    assert "items: created=1" in capsys.readouterr().out


def test_main_exits_non_zero_on_a_conflict(tmp_path, monkeypatch) -> None:
    async def _fake_run_import(*args, **kwargs):
        return {
            "counts": {"items": {"created": 0, "merged": 1, "skipped": 0}},
            "conflicts": [{"type": "item_category", "item": "Milk"}],
        }

    monkeypatch.setattr(pantry_import, "run_import", _fake_run_import)
    code = pantry_import.main([str(_sample_file(tmp_path))])
    assert code == 1


def test_main_allow_conflicts_downgrades_to_zero(tmp_path, monkeypatch) -> None:
    async def _fake_run_import(*args, **kwargs):
        return {
            "counts": {"items": {"created": 0, "merged": 1, "skipped": 0}},
            "conflicts": [{"type": "item_category", "item": "Milk"}],
        }

    monkeypatch.setattr(pantry_import, "run_import", _fake_run_import)
    code = pantry_import.main([str(_sample_file(tmp_path)), "--allow-conflicts"])
    assert code == 0


def test_main_exits_non_zero_on_a_bad_file(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"version": 2}))
    assert pantry_import.main([str(path)]) == 1


def test_main_exits_non_zero_when_the_server_reports_an_error(tmp_path, monkeypatch) -> None:
    async def _fake_run_import(*args, **kwargs):
        raise RuntimeError("items[0].name: required")

    monkeypatch.setattr(pantry_import, "run_import", _fake_run_import)
    assert pantry_import.main([str(_sample_file(tmp_path))]) == 1


# --- in-process: the real server app, over the real MCP transport ---------


def _build_pantry_app(monkeypatch, tmp_path, token: str | None = None):
    if token is None:
        monkeypatch.delenv("ADDON_TOKEN", raising=False)
    else:
        monkeypatch.setenv("ADDON_TOKEN", token)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("PANTRY_DB", str(tmp_path / "pantry.db"))

    from joshua_pantry.server import build_app

    return build_app()


async def test_run_import_drives_the_real_server_app_in_process(monkeypatch, tmp_path) -> None:
    from joshua_pantry.server import mcp as pantry_mcp

    app = _build_pantry_app(monkeypatch, tmp_path)
    data = {
        "version": 1,
        "items": [{"name": "Milk"}, {"name": "Eggs"}, {"name": "Bread"}],
    }

    transport = httpx2.ASGITransport(app=app)
    client = httpx2.AsyncClient(transport=transport, base_url="http://testserver", timeout=10)
    try:
        async with pantry_mcp.session_manager.run():
            totals = await pantry_import.run_import(
                data, "http://testserver/mcp", None, chunk_size=2, http_client=client
            )
    finally:
        await client.aclose()

    assert totals["counts"]["items"] == {"created": 3, "merged": 0, "skipped": 0}
    assert totals["conflicts"] == []


async def test_run_import_raises_on_a_rejected_batch(monkeypatch, tmp_path) -> None:
    """``run_import`` raises the addon's own RuntimeError on a rejected batch.

    The in-process test harness runs the server's own session manager
    around the call, which (like the MCP client) wraps an exception raised
    through it in an ``ExceptionGroup`` — a real CLI run has no such wrapper.
    ``subgroup`` finds the RuntimeError inside, whatever the nesting.
    """
    from joshua_pantry.server import mcp as pantry_mcp

    app = _build_pantry_app(monkeypatch, tmp_path)
    data = {"version": 1, "items": [{"name": "Milk", "bogus": True}]}

    transport = httpx2.ASGITransport(app=app)
    client = httpx2.AsyncClient(transport=transport, base_url="http://testserver", timeout=10)
    try:
        with pytest.raises(BaseExceptionGroup) as excinfo:
            async with pantry_mcp.session_manager.run():
                await pantry_import.run_import(
                    data, "http://testserver/mcp", None, chunk_size=200, http_client=client
                )
        matched = excinfo.value.subgroup(RuntimeError)
        assert matched is not None
        assert "unknown field" in str(_first_leaf(matched))
    finally:
        await client.aclose()


async def test_run_import_sends_the_token_as_a_bearer_header(monkeypatch, tmp_path) -> None:
    """``--token`` becomes the header the addon's own auth middleware checks."""
    from joshua_pantry.server import mcp as pantry_mcp

    app = _build_pantry_app(monkeypatch, tmp_path, token="secret")
    data = {"version": 1, "items": [{"name": "Milk"}]}
    transport = httpx2.ASGITransport(app=app)
    captured: dict[str, Any] = {}

    class _CapturingClient(httpx2.AsyncClient):
        def __init__(self, *args, **kwargs):
            captured["headers"] = kwargs.get("headers")
            kwargs["transport"] = transport
            kwargs["base_url"] = "http://testserver"
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(pantry_import.httpx2, "AsyncClient", _CapturingClient)

    async with pantry_mcp.session_manager.run():
        totals = await pantry_import.run_import(
            data, "http://testserver/mcp", "secret", chunk_size=200
        )

    assert captured["headers"] == {"Authorization": "Bearer secret"}
    assert totals["counts"]["items"] == {"created": 1, "merged": 0, "skipped": 0}
