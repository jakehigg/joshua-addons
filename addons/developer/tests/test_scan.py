"""The credential scan over the added lines of a diff.

gitleaks is not in the test environment. The ``gitleaks`` fixture of
conftest answers each call of the scan with the JSON report of the binary.
Each value with the shape of a credential is built at run time from
fragments, so this file holds no text that a scanner reads as a credential.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
from joshua_developer import scan
from joshua_developer.platforms import Diff, DiffFile
from joshua_developer.scan import ScanUnavailable, added_lines, scan_diff, scan_text

# A value with the shape of a credential: the fake gitleaks of conftest
# reports a finding for each line that holds it.
LEAK = "Qr7" * 14
# Values that the allowlist of the configuration must never allow.
CREDENTIALS = (
    LEAK,
    "A1b2C3d4E5f6G7h8" + "I9j0",
    "x" * 40,
    # An angle bracket that does not close is no placeholder.
    "<" + "y" * 40,
)


def patch_with(line: str) -> str:
    return f"--- a/f\n+++ b/f\n@@ -10,2 +10,3 @@\n context\n+{line}\n context"


def test_a_finding_names_the_rule_the_file_and_the_line(gitleaks) -> None:
    gitleaks.report = [
        {
            "RuleID": "aws-access-token",
            "File": "0/src/app.py",
            "StartLine": 1,
            "Secret": LEAK,
            "Match": f"key = {LEAK}",
        }
    ]
    findings = scan_diff(Diff(files=[DiffFile(path="src/app.py", patch=patch_with(LEAK))]))
    assert [(f.path, f.line_no, f.pattern_name) for f in findings] == [
        ("src/app.py", 11, "aws-access-token")
    ]
    # A finding never holds the matched text, also when the report does.
    assert LEAK not in json.dumps([f.to_dict() for f in findings])
    assert LEAK not in repr(findings)


def test_the_scan_calls_gitleaks_over_the_added_lines(gitleaks) -> None:
    assert scan_text("src/app.py", patch_with(LEAK)) != []
    [args] = gitleaks.calls
    assert args[:2] == [scan.GITLEAKS, "dir"]
    assert args[3:] == [
        "--config",
        str(scan.CONFIG_PATH),
        "--report-format",
        "json",
        "--report-path",
        args[args.index("--report-path") + 1],
        "--exit-code",
        "0",
        "--no-banner",
    ]
    # Only the added line, in a directory for the file, under the path of
    # the file in the repository.
    assert gitleaks.written == {"0/src/app.py": f"{LEAK}\n"}


@pytest.mark.parametrize("path", ["/etc/app.conf", "../../app.conf", ""])
def test_a_path_outside_the_repository_stays_in_the_directory(gitleaks, path: str) -> None:
    assert scan_text(path, patch_with(LEAK)) != []
    [written] = gitleaks.written
    assert written.startswith("0/") and ".." not in written


def test_the_temporary_directory_is_gone_after_the_scan(gitleaks) -> None:
    assert scan_text("f", patch_with(LEAK)) != []
    target = Path(gitleaks.calls[0][2])
    assert not target.exists() and not target.parent.exists()


def test_a_report_line_maps_to_the_line_of_the_new_file(gitleaks) -> None:
    gitleaks.report = [
        {"RuleID": "aws-access-token", "File": "0/src/a.py", "StartLine": 2},
        {"RuleID": "generic-api-key", "File": "1/docs/b.md", "StartLine": 1},
    ]
    diff = Diff(
        files=[
            DiffFile(path="src/a.py", patch="@@ -1 +5,3 @@\n x\n+one\n+two"),
            DiffFile(path="docs/b.md", patch="@@ -0,0 +30 @@\n+only"),
        ]
    )
    assert [(f.path, f.line_no, f.pattern_name) for f in scan_diff(diff)] == [
        ("src/a.py", 6 + 1, "aws-access-token"),
        ("docs/b.md", 30, "generic-api-key"),
    ]


def test_a_file_that_adds_no_line_is_not_scanned(gitleaks) -> None:
    gitleaks.report = [{"RuleID": "gitlab-pat", "File": "0/b.py", "StartLine": 1}]
    diff = Diff(
        files=[
            DiffFile(path="a.py", patch="@@ -1,2 +1 @@\n x\n-y"),
            DiffFile(path="b.py", patch="@@ -0,0 +7 @@\n+leak"),
        ]
    )
    assert [(f.path, f.line_no) for f in scan_diff(diff)] == [("b.py", 7)]


@pytest.mark.parametrize("start_line", [0, 9, "x", None])
def test_a_line_outside_the_file_takes_the_nearest_line(gitleaks, start_line: object) -> None:
    gitleaks.report = [{"RuleID": "private-key", "File": "0/a.py", "StartLine": start_line}]
    [finding] = scan_text("a.py", "@@ -0,0 +3,2 @@\n+one\n+two")
    assert finding.line_no in (3, 4)


def test_two_findings_of_one_rule_on_one_line_are_one_finding(gitleaks) -> None:
    gitleaks.report = [
        {"RuleID": "generic-api-key", "File": "0/a.py", "StartLine": 1, "StartColumn": 1},
        {"RuleID": "generic-api-key", "File": "0/a.py", "StartLine": 1, "StartColumn": 20},
        {"RuleID": "aws-access-token", "File": "0/a.py", "StartLine": 1},
    ]
    findings = scan_text("a.py", "@@ -0,0 +1 @@\n+x")
    assert [(f.line_no, f.pattern_name) for f in findings] == [
        (1, "aws-access-token"),
        (1, "generic-api-key"),
    ]


def test_a_rule_without_a_name_is_still_a_finding(gitleaks) -> None:
    gitleaks.report = [{"File": "0/a.py", "StartLine": 1}]
    [finding] = scan_text("a.py", "@@ -0,0 +1 @@\n+x")
    assert finding.pattern_name == "unknown-rule"


# --- the scan is never clean when it did not read the diff -----------------


def test_a_missing_binary_is_unavailable(gitleaks, monkeypatch) -> None:
    monkeypatch.setattr(scan.shutil, "which", lambda name, *args, **kw: None)
    assert not scan.available()
    with pytest.raises(ScanUnavailable):
        scan_text("f", patch_with("clean"))
    with pytest.raises(ScanUnavailable):
        scan_diff(Diff(files=[DiffFile(path="f", patch=patch_with(LEAK))]))
    # An empty diff is unavailable too, and the binary was never called.
    with pytest.raises(ScanUnavailable):
        scan_diff(Diff(files=[]))
    assert gitleaks.calls == []


@pytest.mark.parametrize(
    "error",
    [OSError("no such file"), PermissionError("denied"), subprocess.TimeoutExpired("gitleaks", 1)],
)
def test_a_binary_that_does_not_run_is_unavailable(gitleaks, error: Exception) -> None:
    gitleaks.error = error
    with pytest.raises(ScanUnavailable):
        scan_text("f", patch_with("clean"))


def test_a_failed_run_is_unavailable(gitleaks) -> None:
    gitleaks.returncode = 2
    with pytest.raises(ScanUnavailable):
        scan_text("f", patch_with(LEAK))


@pytest.mark.parametrize("text", ["not json", '{"RuleID": "x"}', "[3]"])
def test_a_report_that_is_not_findings_is_unavailable(gitleaks, text: str) -> None:
    gitleaks.report_text = text
    with pytest.raises(ScanUnavailable):
        scan_text("f", patch_with("clean"))


@pytest.mark.parametrize("file", ["9/other.py", "other.py", "0/other.py"])
def test_a_report_outside_the_scan_is_unavailable(gitleaks, file: str) -> None:
    gitleaks.report = [{"RuleID": "gitlab-pat", "File": file, "StartLine": 1}]
    with pytest.raises(ScanUnavailable):
        scan_text("f", patch_with("clean"))


def test_a_run_that_writes_no_report_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        scan, "run_gitleaks", lambda args: subprocess.CompletedProcess(args, 0, "", "")
    )
    with pytest.raises(ScanUnavailable):
        scan_text("f", patch_with("clean"))


def test_a_file_the_scan_cannot_write_is_unavailable(gitleaks) -> None:
    with pytest.raises(ScanUnavailable):
        scan_text("x" * 300 + ".py", patch_with("clean"))
    assert gitleaks.calls == []


def test_an_empty_report_is_no_finding(gitleaks) -> None:
    gitleaks.report_text = ""
    assert scan_text("f", patch_with("clean")) == []


# --- the configuration -----------------------------------------------------


def config() -> dict:
    return tomllib.loads(scan.CONFIG_PATH.read_text(encoding="utf-8"))


def allowlist_regexes() -> list[re.Pattern[str]]:
    return [re.compile(regex) for regex in config()["allowlist"]["regexes"]]


def test_the_configuration_extends_the_gitleaks_default() -> None:
    assert config()["extend"]["useDefault"] is True


@pytest.mark.parametrize(
    "value",
    [
        "<home assistant long-lived token, set by the Ansible role>",
        "<YOUR_API_KEY_123>",
        "Your-Password Goes Here 123",
        "set your token in .env before you start",
    ],
)
def test_the_allowlist_holds_the_placeholders(value: str) -> None:
    assert any(regex.search(value) for regex in allowlist_regexes())


@pytest.mark.parametrize("value", sorted(CREDENTIALS))
def test_the_allowlist_holds_no_credential(value: str) -> None:
    assert not any(regex.search(value) for regex in allowlist_regexes())


def test_the_allowlist_holds_no_key_block() -> None:
    # A key on one line has long words in it, so the prose pattern misses it.
    header = "-----" + "BEG" + "IN RSA PRIV" + "ATE KEY-----"
    one_line = f"{header} {LEAK}"
    assert not any(regex.search(one_line) for regex in allowlist_regexes())


def test_the_allowlist_holds_the_path_of_the_tests_of_the_scan() -> None:
    # The tests of this addon hold values with the shape of a credential,
    # and gitleaks reads the path of a file, so the path is allowlisted.
    paths = [re.compile(regex) for regex in config()["allowlist"]["paths"]]
    assert any(regex.search("addons/developer/tests/test_scan.py") for regex in paths)
    assert not any(regex.search("addons/developer/joshua_developer/scan.py") for regex in paths)


# --- the added lines -------------------------------------------------------


def test_removed_and_context_lines_are_not_scanned() -> None:
    patch = f"@@ -1,3 +1,2 @@\n-{LEAK}\n {LEAK}\n+clean"
    assert scan_text("f", patch) == []


def test_line_numbers_follow_the_hunks() -> None:
    patch = (
        "diff --git a/f b/f\nindex 1..2 100644\n--- a/f\n+++ b/f\n"
        "@@ -1,2 +1,3 @@\n a\n+b\n c\n"
        f"@@ -40,2 +41,4 @@\n x\n-y\n+{LEAK}\n+z\n\n+{LEAK}\n"
    )
    assert [f.line_no for f in scan_text("f", patch)] == [42, 45]


def test_an_added_line_that_starts_with_plus_signs_is_scanned() -> None:
    [finding] = scan_text("f", f"@@ -1 +1,2 @@\n x\n+++ {LEAK}")
    assert finding.line_no == 2


def test_a_patch_with_no_hunk_header_counts_from_1() -> None:
    assert added_lines("+a\n b\n+c") == [(1, "a"), (3, "c")]


def test_scan_diff_covers_every_file() -> None:
    diff = Diff(
        files=[
            DiffFile(path="a", patch=patch_with("clean")),
            DiffFile(path="b", patch=patch_with(LEAK)),
        ]
    )
    assert [f.path for f in scan_diff(diff)] == ["b"]
    assert scan_diff(Diff(unavailable=True)) == []
