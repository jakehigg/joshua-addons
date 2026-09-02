#!/usr/bin/env python3
"""Drive the pantry addon's ``import_data`` tool from a JSON file.

Reads one import file (``addons/pantry/docs/import.md`` has the format),
splits each section into chunks, and sends one ``import_data`` call per
chunk over streamable HTTP. Prints a summary of the counts every call
returned, folded into one total, and exits non-zero when the import failed
or reported a conflict.

Usage::

    python3 scripts/pantry_import.py my_export.json --url http://localhost:8000/mcp

Run from the repository root, in the workspace venv (``uv run``), so the
``mcp`` client library is on the path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

DEFAULT_URL = "http://localhost:8000/mcp"
DEFAULT_CHUNK_SIZE = 200
IMPORT_VERSION = 1
TOP_LEVEL_FIELDS = frozenset({"version", "items", "purchases", "consumptions"})
SECTIONS = ("items", "purchases", "consumptions")


def load_batch(path: Path) -> dict[str, Any]:
    """Read the import file and check its top level.

    Raises ``ValueError`` naming the problem when the file is not one JSON
    object, holds an unknown top-level field, names an unsupported version,
    or gives a section that is not a list.
    """
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("the import file must hold one JSON object")

    unknown = set(data) - TOP_LEVEL_FIELDS
    if unknown:
        raise ValueError(f"unknown top-level field {sorted(unknown)[0]!r}")

    if data.get("version") != IMPORT_VERSION:
        raise ValueError(
            f"unsupported version {data.get('version')!r}; only version {IMPORT_VERSION} works"
        )

    for section in SECTIONS:
        if section in data and not isinstance(data[section], list):
            raise ValueError(f"{section} must be a list")

    return data


def chunk_rows(rows: list[Any], size: int) -> list[list[Any]]:
    """Split ``rows`` into pieces of at most ``size`` rows each."""
    if not rows:
        return []
    return [rows[i : i + size] for i in range(0, len(rows), size)]


def build_calls(data: dict[str, Any], chunk_size: int) -> list[dict[str, Any]]:
    """Build one ``import_data`` call payload per chunk.

    Every call carries one section, so a large dataset streams as several
    small calls instead of one call that never finishes.
    """
    calls: list[dict[str, Any]] = []
    for section in SECTIONS:
        for piece in chunk_rows(data.get(section) or [], chunk_size):
            calls.append({"version": data["version"], section: piece})
    return calls


def new_totals() -> dict[str, Any]:
    return {"counts": {}, "conflicts": []}


def merge_summary(totals: dict[str, Any], result: dict[str, Any]) -> None:
    """Fold one call's ``import_data`` result into the running totals."""
    for section, counts in result.get("counts", {}).items():
        section_totals = totals["counts"].setdefault(section, {})
        for key, value in counts.items():
            section_totals[key] = section_totals.get(key, 0) + value
    totals["conflicts"].extend(result.get("conflicts", []))


def format_summary(totals: dict[str, Any]) -> str:
    lines = ["import summary:"]
    for section, counts in totals["counts"].items():
        parts = ", ".join(f"{key}={value}" for key, value in counts.items())
        lines.append(f"  {section}: {parts}")
    lines.append(f"  conflicts: {len(totals['conflicts'])}")
    for conflict in totals["conflicts"]:
        lines.append(f"    - {conflict}")
    return "\n".join(lines)


async def run_import(
    data: dict[str, Any],
    url: str,
    token: str | None,
    chunk_size: int,
    http_client: httpx2.AsyncClient | None = None,
) -> dict[str, Any]:
    """Send every chunk of ``data`` to a pantry addon over MCP, in order.

    ``http_client``, when given, replaces the default HTTP client — a test
    passes one built on an ASGI transport to drive a real server app
    in-process, with no network.
    """
    calls = build_calls(data, chunk_size)
    totals = new_totals()

    owns_client = http_client is None
    client = http_client or httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"} if token else {}, timeout=30
    )
    try:
        # The MCP client runs its own background task groups, so a plain
        # ``raise`` inside the ``async with`` blocks below surfaces here as
        # an ``ExceptionGroup``, not the RuntimeError itself. ``except*``
        # unwraps it back to the one error a caller (main, a test) checks.
        try:
            async with streamable_http_client(url, http_client=client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    for call in calls:
                        response = await session.call_tool("import_data", call)
                        if response.is_error:
                            [block] = response.content
                            raise RuntimeError(block.text)
                        merge_summary(totals, response.structured_content)
        except* RuntimeError as eg:
            raise eg.exceptions[0] from None
    finally:
        if owns_client:
            await client.aclose()

    return totals


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bulk-import pantry history over MCP.")
    parser.add_argument("path", type=Path, help="the import JSON file")
    parser.add_argument("--url", default=DEFAULT_URL, help="the pantry MCP endpoint")
    parser.add_argument("--token", default=None, help="the ADDON_TOKEN, when the addon needs one")
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help="rows per section, per import_data call",
    )
    parser.add_argument(
        "--allow-conflicts",
        action="store_true",
        help="exit 0 even when the import reports a conflict",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        data = load_batch(args.path)
        totals = asyncio.run(run_import(data, args.url, args.token, args.chunk_size))
    except Exception as exc:
        print(f"pantry-import: {exc}", file=sys.stderr)
        return 1

    print(format_summary(totals))
    if totals["conflicts"] and not args.allow_conflicts:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
