#!/usr/bin/env python3
"""Print the addon names a CI matrix must cover.

An addon is a directory ``addons/<name>/`` that holds a ``Dockerfile``. That
file is what puts the addon in the image build, so its presence is the only
rule (see ``CLAUDE.md``, "The addon contract"). Print a JSON array of the
names, sorted, so a workflow step can turn it into a matrix with
``fromJSON(...)``. No argument, no config file, stdlib only.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADDONS_DIR = ROOT / "addons"


def list_addons() -> list[str]:
    """Return every addon name under ``addons/`` that has a ``Dockerfile``."""
    if not ADDONS_DIR.is_dir():
        return []
    return sorted(
        path.name
        for path in ADDONS_DIR.iterdir()
        if path.is_dir() and (path / "Dockerfile").is_file()
    )


def main() -> int:
    print(json.dumps(list_addons()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
