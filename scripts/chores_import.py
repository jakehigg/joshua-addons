#!/usr/bin/env python3
"""Send a chores import file to the ``import_data`` tool of the chores addon.

Reads one import file (``addons/chores/docs/import.md`` has the format)
and sends it over MCP streamable HTTP in more than one call:

1. The first call sends all the members and all the chores.
2. The next calls send the completions, in batches of ``--batch-size`` rows.
3. The last calls send the ledger rows, in batches of ``--batch-size`` rows.

A large ledger thus does not become one large request. Each call commits
by itself. A second run of the same file changes nothing, so after a
failure you can run the same command again.

The script prints the counts of all the calls and the last balance that
the addon gave for each member. It exits with code 1 when a call fails.

Usage::

    uv run python scripts/chores_import.py chores_export.json \\
      --url http://localhost:8000/mcp --token <ADDON_TOKEN>

Run from the repository root with ``uv run``, so that the ``mcp`` client
library is on the path.
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
DEFAULT_BATCH_SIZE = 500
IMPORT_VERSION = 1
SECTIONS = ("members", "chores", "completions", "transactions")
TOP_LEVEL_FIELDS = frozenset({"version", *SECTIONS})
BATCHED_SECTIONS = ("completions", "transactions")


def load_batch(path: Path) -> dict[str, Any]:
    """Read the import file and check its top level.

    Raises ``ValueError`` when the file is not one JSON object, has an
    unknown top-level field, gives a version that is not supported, or
    gives a section that is not a list.
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
    """Split ``rows`` into pieces of ``size`` rows or less."""
    if not rows:
        return []
    return [rows[i : i + size] for i in range(0, len(rows), size)]


def build_calls(data: dict[str, Any], batch_size: int) -> list[dict[str, Any]]:
    """Return one ``import_data`` payload for each call, in the order to send them.

    The tool needs ``members`` in each call, so each later call gives an
    empty list.
    """
    version = data["version"]
    calls: list[dict[str, Any]] = [
        {
            "version": version,
            "members": data.get("members") or [],
            "chores": data.get("chores") or [],
        }
    ]
    for section in BATCHED_SECTIONS:
        for piece in chunk_rows(data.get(section) or [], batch_size):
            calls.append({"version": version, "members": [], section: piece})
    return calls


def new_totals() -> dict[str, Any]:
    return {"calls": 0, "counts": {}, "balances": {}}


def merge_result(totals: dict[str, Any], result: dict[str, Any]) -> None:
    """Add the result of one call to the totals.

    A later call gives a later balance, so it replaces the earlier one.
    """
    totals["calls"] += 1
    for section, counts in result.get("counts", {}).items():
        section_totals = totals["counts"].setdefault(section, {})
        for key, value in counts.items():
            section_totals[key] = section_totals.get(key, 0) + value
    totals["balances"].update(result.get("balances", {}))


def format_summary(totals: dict[str, Any]) -> str:
    lines = [f"import summary ({totals['calls']} calls):"]
    for section, counts in totals["counts"].items():
        parts = ", ".join(f"{key}={value}" for key, value in counts.items())
        lines.append(f"  {section}: {parts}")
    lines.append("balances:")
    for slug, value in totals["balances"].items():
        lines.append(f"  {slug}: {value}")
    return "\n".join(lines)


async def run_import(
    data: dict[str, Any],
    url: str,
    token: str | None,
    batch_size: int,
    http_client: httpx2.AsyncClient | None = None,
) -> dict[str, Any]:
    """Send each call of ``data`` to a chores addon over MCP, in order.

    A test can give ``http_client`` with an ASGI transport, to drive a real
    server app in the same process with no network.
    """
    calls = build_calls(data, batch_size)
    totals = new_totals()

    owns_client = http_client is None
    client = http_client or httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"} if token else {}, timeout=120
    )
    try:
        # The MCP client runs its own task groups. A ``raise`` in the blocks
        # below comes out as an ``ExceptionGroup``. ``except*`` gives the
        # caller the RuntimeError itself.
        try:
            async with streamable_http_client(url, http_client=client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    for call in calls:
                        response = await session.call_tool("import_data", call)
                        if response.is_error:
                            text = " ".join(
                                getattr(block, "text", "") for block in response.content
                            )
                            raise RuntimeError(text or "import_data failed")
                        merge_result(totals, response.structured_content)
        except* RuntimeError as eg:
            raise eg.exceptions[0] from None
    finally:
        if owns_client:
            await client.aclose()

    return totals


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bulk-import chores data over MCP.")
    parser.add_argument("path", type=Path, help="the import JSON file")
    parser.add_argument("--url", default=DEFAULT_URL, help="the MCP endpoint of the chores addon")
    parser.add_argument("--token", default=None, help="the ADDON_TOKEN, when the addon needs one")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="completions or ledger rows in each import_data call",
    )
    return parser.parse_args(argv)


def _first_leaf(exc: BaseException) -> BaseException:
    """Return the first error in an ``ExceptionGroup``, at any depth."""
    while isinstance(exc, BaseExceptionGroup):
        exc = exc.exceptions[0]
    return exc


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.batch_size < 1:
        print("chores-import: --batch-size must be 1 or more", file=sys.stderr)
        return 1
    try:
        data = load_batch(args.path)
        totals = asyncio.run(run_import(data, args.url, args.token, args.batch_size))
    except Exception as exc:
        print(f"chores-import: {_first_leaf(exc)}", file=sys.stderr)
        return 1

    print(format_summary(totals))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
