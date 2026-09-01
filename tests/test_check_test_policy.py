"""Tests for scripts/check_test_policy.py: the test-hygiene guard for addons and root scripts."""

from __future__ import annotations

from pathlib import Path

from scripts import check_test_policy

ROOT = Path(__file__).resolve().parent.parent


def test_the_committed_tree_is_clean() -> None:
    assert check_test_policy.main() == 0


def test_finds_every_addon_member() -> None:
    members = check_test_policy._addon_members(ROOT)
    assert members == sorted(members)
    assert "hello" in members


def _make_sample_addon(root: Path, test_body: str) -> None:
    package_dir = root / "addons" / "sample" / "joshua_sample"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("")
    (package_dir / "widget.py").write_text("VALUE = 1\n")
    (root / "addons" / "sample" / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    tests_dir = root / "addons" / "sample" / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_widget.py").write_text(test_body)
    (root / "scripts").mkdir()
    (root / "tests").mkdir()


def test_a_module_with_no_importing_test_is_a_violation(tmp_path) -> None:
    _make_sample_addon(tmp_path, "")
    problems = check_test_policy.check_module_coverage(tmp_path)
    assert any("joshua_sample.widget" in problem for problem in problems)


def test_a_module_with_an_importing_test_passes(tmp_path) -> None:
    _make_sample_addon(tmp_path, "from joshua_sample import widget\n")
    assert check_test_policy.check_module_coverage(tmp_path) == []


def test_a_root_script_with_no_importing_test_is_a_violation(tmp_path) -> None:
    _make_sample_addon(tmp_path, "from joshua_sample import widget\n")
    (tmp_path / "scripts" / "tool.py").write_text("VALUE = 1\n")
    problems = check_test_policy.check_module_coverage(tmp_path)
    assert any("scripts.tool" in problem for problem in problems)


def test_a_root_script_with_an_importing_test_passes(tmp_path) -> None:
    _make_sample_addon(tmp_path, "from joshua_sample import widget\n")
    (tmp_path / "scripts" / "tool.py").write_text("VALUE = 1\n")
    (tmp_path / "tests" / "test_tool.py").write_text("from scripts import tool\n")
    assert check_test_policy.check_module_coverage(tmp_path) == []


# Built with a join, not one literal, so this file's own text never spells out
# a bare `pytest.mark.skip(` — that would make check_test_policy flag itself
# when it scans this file, since the check works on raw text, not on what the
# marker does at runtime.
_SKIP_MARKER = "pytest.mark." + "skip"


def test_skip_with_no_reason_is_a_violation(tmp_path) -> None:
    test_file = tmp_path / "test_skips.py"
    test_file.write_text(f"import pytest\n\n@{_SKIP_MARKER}()\ndef test_x(): pass\n")
    problems = check_test_policy.check_skip_reasons([test_file])
    assert problems


def test_skip_with_an_issue_reason_passes(tmp_path) -> None:
    test_file = tmp_path / "test_skips.py"
    test_file.write_text(
        f'import pytest\n\n@{_SKIP_MARKER}(reason="flaky, see #12")\ndef test_x(): pass\n'
    )
    assert check_test_policy.check_skip_reasons([test_file]) == []
