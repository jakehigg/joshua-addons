"""The token scan over the added lines of a diff.

Each fake credential is built at run time, so this file holds no text that
looks like a real one.
"""

from __future__ import annotations

import json

import pytest
from joshua_developer.platforms import Diff, DiffFile
from joshua_developer.scan import added_lines, scan_diff, scan_text

FAKES = {
    "anthropic_key": "sk-" + "ant-" + "api03-" + "A1b2C3d4E5f6G7h8",
    "gitlab_token": "gl" + "pat-" + "x" * 20,
    "github_token": "gh" + "p_" + "A" * 36,
    "github_fine_grained_token": "github" + "_pat_" + "11AAAA" + "b" * 30,
    "github_oauth_token": "gh" + "o_" + "C" * 36,
    "slack_bot_token": "xo" + "xb-" + "1234-5678-abcdef",
    "slack_user_token": "xo" + "xp-" + "1234-5678-abcdef",
    "aws_access_key": "AK" + "IA" + "ABCDEFGHIJKLMNOP",
    "private_key": "-----BEGIN " + "RSA PRIVATE KEY-----",
    "assigned_secret": "api_key = '" + "qR7" * 7 + "'",
}


def patch_with(line: str) -> str:
    return f"--- a/f\n+++ b/f\n@@ -10,2 +10,3 @@\n context\n+{line}\n context"


@pytest.mark.parametrize(("name", "value"), sorted(FAKES.items()))
def test_every_pattern_finds_its_credential(name: str, value: str) -> None:
    findings = scan_diff(Diff(files=[DiffFile(path="src/app.py", patch=patch_with(value))]))
    assert [(f.path, f.line_no, f.pattern_name) for f in findings] == [("src/app.py", 11, name)]
    # A finding never holds the matched text.
    assert value not in json.dumps([f.to_dict() for f in findings])
    assert value not in repr(findings)


@pytest.mark.parametrize(
    "header",
    ["OPENSSH PRIVATE KEY", "EC PRIVATE KEY", "PGP PRIVATE KEY BLOCK", "PRIVATE KEY"],
)
def test_the_private_key_forms(header: str) -> None:
    [finding] = scan_text("k", patch_with("-----BEGIN " + header + "-----"))
    assert finding.pattern_name == "private_key"


@pytest.mark.parametrize(
    "line",
    [
        'password: "' + "pW9" * 6 + '"',
        'CLIENT_SECRET="' + "s-3" * 10 + '"',
        "auth_token = '" + "T_t" * 6 + "'",
        'API-KEY: "' + "K9k" * 6 + '"',
    ],
)
def test_the_assigned_secret_forms(line: str) -> None:
    [finding] = scan_text("f", patch_with(line))
    assert finding.pattern_name == "assigned_secret"


@pytest.mark.parametrize(
    "line",
    [
        "Set your token in .env before you start.",
        "The password must be at least 12 characters long.",
        'token = "short"',
        'TOKEN_PREFIXES = ("sk-' + 'ant-", "gl' + 'pat-", "gh' + 'p_")',
        "Keys start with AK" + "IA and are 20 characters.",
        "-----BEGIN PUBLIC KEY-----",
        # Test fixtures: long, but with fewer than three character classes.
        'password = "' + "." * 16 + '"',
        'token = "' + "x" * 20 + '"',
        "api_key = '" + "q" * 20 + "'",
        'secret = "' + "a1" * 10 + '"',
    ],
)
def test_ordinary_text_does_not_match(line: str) -> None:
    assert scan_text("README.md", patch_with(line)) == []


def test_removed_and_context_lines_are_not_scanned() -> None:
    secret = FAKES["github_token"]
    patch = f"@@ -1,3 +1,2 @@\n-{secret}\n {secret}\n+clean"
    assert scan_text("f", patch) == []


def test_line_numbers_follow_the_hunks() -> None:
    secret = FAKES["aws_access_key"]
    patch = (
        "diff --git a/f b/f\nindex 1..2 100644\n--- a/f\n+++ b/f\n"
        "@@ -1,2 +1,3 @@\n a\n+b\n c\n"
        f"@@ -40,2 +41,4 @@\n x\n-y\n+{secret}\n+z\n\n+{secret}\n"
    )
    findings = scan_text("f", patch)
    assert [f.line_no for f in findings] == [42, 45]


def test_an_added_line_that_starts_with_plus_signs_is_scanned() -> None:
    secret = FAKES["gitlab_token"]
    [finding] = scan_text("f", f"@@ -1 +1,2 @@\n x\n+++ {secret}")
    assert finding.line_no == 2


def test_a_patch_with_no_hunk_header_counts_from_1() -> None:
    assert added_lines("+a\n b\n+c") == [(1, "a"), (3, "c")]


def test_one_line_with_two_patterns_gives_two_findings() -> None:
    line = FAKES["github_token"] + " " + FAKES["aws_access_key"]
    names = sorted(f.pattern_name for f in scan_text("f", patch_with(line)))
    assert names == ["aws_access_key", "github_token"]


def test_scan_diff_covers_every_file() -> None:
    diff = Diff(
        files=[
            DiffFile(path="a", patch=patch_with("clean")),
            DiffFile(path="b", patch=patch_with(FAKES["slack_bot_token"])),
        ]
    )
    assert [f.path for f in scan_diff(diff)] == ["b"]
    assert scan_diff(Diff(unavailable=True)) == []
