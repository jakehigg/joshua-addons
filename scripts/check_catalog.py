"""Refuse a tree where ``catalog.yaml`` and ``addons/`` disagree.

The catalog is what a reader on another machine learns about the addons: the
joshua-ai ``instance`` builtin reads it at the latest release. So the file must
name every addon and nothing else, and each entry must say what the addon
says about itself.

The rules:

* every ``addons/<name>/`` with a ``Dockerfile`` and a package that builds an
  ``MCPServer`` has one entry, and every entry has such a directory. A second
  image that serves no MCP, such as ``developer-worker``, is not an addon and
  has no entry.
* ``server`` (or ``name`` when ``server`` is absent) is the string the addon
  passes as ``MCPServer(name=...)``.
* ``summary`` is one line, 1 to 120 characters.
* ``since`` and, when present, ``requires_joshua_ai`` are of the date shape.

Run from the repository root. Exit 0 when clean, 1 when a rule is broken.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog.yaml"
ADDONS_DIR = ROOT / "addons"

VERSION_SHAPE = re.compile(r"^\d{4}\.(1[0-2]|[1-9])\.[1-9]\d*$")
SERVER_NAME_RE = re.compile(r"""MCPServer\(\s*name\s*=\s*["']([^"']+)["']""")
MAX_SUMMARY = 120


def addon_dirs(addons_dir: Path = ADDONS_DIR) -> dict[str, str]:
    """``name -> MCPServer name`` for every addon directory with a Dockerfile and a
    ``joshua_<name>`` package that builds an ``MCPServer``. A directory whose
    package builds none serves no MCP, so it is a second image and not an addon."""
    out: dict[str, str] = {}
    if not addons_dir.is_dir():
        return out
    for path in sorted(addons_dir.iterdir()):
        if not path.is_dir() or not (path / "Dockerfile").is_file():
            continue
        package = path / f"joshua_{path.name.replace('-', '_')}"
        if not package.is_dir():
            continue
        server = _server_name(package)
        if server is not None:
            out[path.name] = server
    return out


def _server_name(package: Path) -> str | None:
    for source in sorted(package.glob("*.py")):
        match = SERVER_NAME_RE.search(source.read_text())
        if match:
            return match.group(1)
    return None


def check(catalog_text: str, dirs: dict[str, str]) -> list[str]:
    """Every problem, one line each. Empty when the catalog is right."""
    import yaml

    try:
        data = yaml.safe_load(catalog_text) or {}
    except yaml.YAMLError as exc:
        return [f"catalog.yaml cannot be read: {exc}"]
    entries = data.get("addons") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return ["catalog.yaml must hold a list under 'addons'"]

    problems: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("name"):
            problems.append("an entry has no 'name'")
            continue
        name = str(entry["name"])
        seen.add(name)
        if name not in dirs:
            problems.append(f"{name}: no addons/{name}/ with a Dockerfile and a package")
        else:
            want = str(entry.get("server") or name)
            have = dirs[name]
            if have != want:
                problems.append(
                    f"{name}: 'server' is '{want}' but the package names its MCPServer '{have}'"
                )
        summary = str(entry.get("summary") or "").strip()
        if not summary or len(summary) > MAX_SUMMARY or "\n" in summary:
            problems.append(f"{name}: 'summary' must be one line of 1 to {MAX_SUMMARY} characters")
        for key in ("since", "requires_joshua_ai"):
            value = entry.get(key)
            if key == "since" and value is None:
                problems.append(f"{name}: 'since' is missing")
            elif value is not None and not VERSION_SHAPE.match(str(value)):
                problems.append(f"{name}: '{key}' is '{value}', not YYYY.M.N")
        for key in ("image", "docs"):
            if not entry.get(key):
                problems.append(f"{name}: '{key}' is missing")
    for name in sorted(set(dirs) - seen):
        problems.append(f"{name}: addons/{name}/ has no entry in catalog.yaml")
    return problems


def main() -> int:
    if not CATALOG.is_file():
        print("catalog: catalog.yaml is missing", file=sys.stderr)
        return 1
    problems = check(CATALOG.read_text(), addon_dirs())
    if problems:
        print("catalog: catalog.yaml and addons/ disagree.", file=sys.stderr)
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"catalog: clean ({len(addon_dirs())} addons)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
