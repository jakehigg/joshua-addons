"""The two commands: ``serve`` (the default) and ``import``.

``python -m joshua_chores`` serves MCP, the HTTP API, and the UI on port
8000. ``python -m joshua_chores serve`` does the same.

``python -m joshua_chores import <path>`` loads one version 1 import file
(``docs/import.md``) into the database of the addon, and then exits. A path
of ``-`` reads the file from stdin. The command uses the same database as
the server: ``DATABASE_URL``, or the SQLite file at ``CHORES_DB``. It runs
the migrations first. The import is one transaction: it writes all the
rows, or it writes nothing.

The import writes the result to stdout as JSON: the ``created`` and
``skipped`` counts of each table, and the balance of each member in the
file. The exit codes are:

- 0: the import is done.
- 1: the import rejected the file, or a setting is not valid. Each problem
  is one line on stderr. The command wrote nothing.
- 2: the file is not a version 1 import file: it is not JSON, it is not
  one JSON object, or the top level is not correct. The command wrote
  nothing.

An import is an operator action. No MCP tool gives it to the agent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from joshua_chores import importer
from joshua_chores import settings as settings_module
from joshua_chores.database import build_engine, build_sessionmaker
from joshua_chores.log import configure_from_env
from joshua_chores.migrations import run_migrations
from joshua_chores.service import InvalidArgument

SERVICE = "joshua-chores"
SECTIONS = ("members", "chores", "completions", "transactions")
TOP_LEVEL_FIELDS = frozenset({"version", *SECTIONS})

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_BAD_FILE = 2

STDIN_PATH = "-"


class BadFile(ValueError):
    """The file is not a version 1 import file."""


def read_document(text: str) -> dict[str, Any]:
    """Parse ``text`` and check the top level of the import file.

    Raises ``BadFile`` when ``text`` is not JSON, is not one JSON object,
    has an unknown top-level field, has no ``members``, gives a version
    that is not 1, or gives a section that is not a list. The message never
    holds the content of the file.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BadFile(f"the file is not JSON (line {exc.lineno}, column {exc.colno})") from None
    if not isinstance(data, dict):
        raise BadFile("the file must hold one JSON object")
    unknown = sorted(set(data) - TOP_LEVEL_FIELDS)
    if unknown:
        raise BadFile(f"unknown top-level field {unknown[0]!r}")
    version = data.get("version")
    if type(version) is not int or version != importer.FORMAT_VERSION:
        raise BadFile(f"version must be {importer.FORMAT_VERSION}")
    if "members" not in data:
        raise BadFile("members is necessary; it can be an empty list")
    for section in SECTIONS:
        if section in data and not isinstance(data[section], list):
            raise BadFile(f"{section} must be a list")
    return data


def _read_text(path: str) -> str:
    if path == STDIN_PATH:
        return sys.stdin.read()
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BadFile(f"cannot read {path}: {exc.__class__.__name__}") from None


async def run_import(document: dict[str, Any]) -> dict[str, Any]:
    """Run the migrations, then import ``document`` in one session and one transaction."""
    engine = build_engine()
    try:
        await run_migrations(engine)
        async with build_sessionmaker(engine)() as session:
            return await importer.import_data(
                session,
                document["version"],
                document.get("members"),
                document.get("chores"),
                document.get("completions"),
                document.get("transactions"),
            )
    finally:
        await engine.dispose()


def import_command(path: str) -> int:
    """Import the file at ``path``, or stdin when ``path`` is ``-``. Return the exit code."""
    configure_from_env(SERVICE, stream=sys.stderr)
    try:
        document = read_document(_read_text(path))
    except BadFile as exc:
        print(f"chores import: {exc}", file=sys.stderr)
        return EXIT_BAD_FILE
    try:
        settings_module.load()
        result = asyncio.run(run_import(document))
    except InvalidArgument as exc:
        for line in str(exc).splitlines():
            print(line, file=sys.stderr)
        return EXIT_REJECTED
    except ValueError as exc:
        print(f"chores import: {exc}", file=sys.stderr)
        return EXIT_REJECTED
    print(json.dumps(result, indent=2))
    return EXIT_OK


def serve_command() -> int:
    """Serve MCP, the HTTP API, and the UI on port 8000 until the process stops."""
    import uvicorn

    from joshua_chores.server import build_app

    configure_from_env(SERVICE)
    uvicorn.run(build_app(), host="0.0.0.0", port=8000, log_config=None)
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m joshua_chores",
        description="The chores addon. With no command, it serves on port 8000.",
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="serve MCP, the HTTP API, and the UI on port 8000")
    load = commands.add_parser("import", help="load a version 1 import file into the database")
    load.add_argument("path", help="the import file, or - to read stdin")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command in ``argv``. Return the exit code."""
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.command == "import":
        return import_command(args.path)
    return serve_command()
