#!/usr/bin/env python3
"""Fail the pipeline on two test-hygiene violations.

1. A `skip` or `xfail` marker with no `reason` that names an issue (`#<number>`).
2. A source module with no test that imports it: every addon package under
   `addons/*/joshua_<name>/`, and every script under the root `scripts/`.

An addon is discovered from the tree, the same way `scripts/list_addons.py`
finds one, so a new addon needs no edit here.

Run from the repository root. Exit 0 when clean, 1 when a rule is broken.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

EXCLUDED_MODULES = ("__init__.py", "__main__.py")
SKIP_XFAIL = re.compile(r"pytest\.mark\.skip\(|xfail\(")
REASON_WITH_ISSUE = re.compile(r"""reason\s*=\s*(['"])(?:(?!\1).)*#\d+""")


def _call_body(text: str, open_paren: int) -> str:
    """Return the text between the parentheses of a call, parentheses balanced."""
    depth = 0
    for index in range(open_paren, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1 : index]
    return text[open_paren + 1 :]


def _addon_members(root: Path) -> list[str]:
    """Return every addon name, sorted: a directory under `addons/` with a `pyproject.toml`."""
    addons_dir = root / "addons"
    if not addons_dir.is_dir():
        return []
    return sorted(
        path.name
        for path in addons_dir.iterdir()
        if path.is_dir() and (path / "pyproject.toml").is_file()
    )


def check_skip_reasons(test_files: list[Path]) -> list[str]:
    problems: list[str] = []
    for path in test_files:
        text = path.read_text(encoding="utf-8")
        for match in SKIP_XFAIL.finditer(text):
            open_paren = text.index("(", match.start())
            body = _call_body(text, open_paren)
            if not REASON_WITH_ISSUE.search(body):
                line = text.count("\n", 0, match.start()) + 1
                problems.append(
                    f'{path}:{line}: {match.group().rstrip("(")} needs reason="… #<issue>"'
                )
    return problems


def _imported_modules(path: Path) -> set[str]:
    """Return every module name a test file imports, with dotted parents."""
    names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            for alias in node.names:
                names.add(f"{node.module}.{alias.name}")
    return names


def check_module_coverage(root: Path) -> list[str]:
    problems: list[str] = []
    for name in _addon_members(root):
        package = f"joshua_{name}"
        package_dir = root / "addons" / name / package
        tests_dir = root / "addons" / name / "tests"
        if not package_dir.is_dir():
            continue
        imported: set[str] = set()
        for test_file in sorted(tests_dir.rglob("test_*.py")):
            imported |= _imported_modules(test_file)
        for source in sorted(package_dir.rglob("*.py")):
            if source.name in EXCLUDED_MODULES:
                continue
            parts = source.relative_to(package_dir).with_suffix("").parts
            module = ".".join((package, *parts))
            if module not in imported:
                problems.append(f"{source}: no test in addons/{name}/tests imports {module}")

    # The root scripts are the CI matrix script and the local checks. The root
    # `tests/` suite is where they get their coverage.
    scripts_dir = root / "scripts"
    tests_dir = root / "tests"
    imported = set()
    if tests_dir.is_dir():
        for test_file in sorted(tests_dir.rglob("test_*.py")):
            imported |= _imported_modules(test_file)
    if scripts_dir.is_dir():
        for source in sorted(scripts_dir.glob("*.py")):
            if source.name in EXCLUDED_MODULES:
                continue
            module = f"scripts.{source.stem}"
            if module not in imported:
                problems.append(f"{source}: no test in tests imports {module}")
    return problems


def _project_test_files(root: Path) -> list[Path]:
    """Return the test files under each addon's own tests directory, plus the root suite.

    Only project test directories are scanned. Dependency and build-artifact
    directories (`.uv-cache`, `.venv`, `.git`) hold vendored test suites that
    the policy must never flag.
    """
    files: list[Path] = []
    for name in _addon_members(root):
        tests_dir = root / "addons" / name / "tests"
        if tests_dir.is_dir():
            files.extend(sorted(tests_dir.rglob("test_*.py")))
    root_tests_dir = root / "tests"
    if root_tests_dir.is_dir():
        files.extend(sorted(root_tests_dir.rglob("test_*.py")))
    return files


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    test_files = _project_test_files(root)
    problems = check_skip_reasons(test_files) + check_module_coverage(root)
    if problems:
        print("test-policy: violations found:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print("test-policy: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
