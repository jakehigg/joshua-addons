"""The token scan: the gate before a pull request.

After a worker pushes, the manager reads the diff over the platform API and
scans the added lines for text that looks like a credential. A finding names
the file, the line in the new file, and the pattern. It never holds the text
that matched, so a finding is safe to store, log, and send to a chat.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from joshua_developer.platforms import Diff

# Each prefix pattern needs a tail of token characters, so code that only
# names a prefix (such as a list of prefixes) does not match.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}")),
    ("gitlab_token", re.compile(r"glpat-[A-Za-z0-9_-]{10,}")),
    ("github_token", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("github_fine_grained_token", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("github_oauth_token", re.compile(r"gho_[A-Za-z0-9]{20,}")),
    ("slack_bot_token", re.compile(r"xoxb-[A-Za-z0-9-]{10,}")),
    ("slack_user_token", re.compile(r"xoxp-[A-Za-z0-9-]{10,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    (
        "private_key",
        re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY(?: BLOCK)?-----"),
    ),
    (
        "assigned_secret",
        re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]{16,}"),
    ),
)

_HUNK_RE = re.compile(r"^@@ -[0-9]+(?:,[0-9]+)? \+([0-9]+)(?:,[0-9]+)? @@")


@dataclass(frozen=True)
class Finding:
    path: str
    line_no: int
    pattern_name: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def added_lines(patch: str) -> list[tuple[int, str]]:
    """The added lines of a unified diff, with their line numbers in the new file."""
    out: list[tuple[int, str]] = []
    line_no = 0
    in_hunk = False
    for line in patch.splitlines():
        hunk = _HUNK_RE.match(line)
        if hunk:
            line_no = int(hunk.group(1))
            in_hunk = True
            continue
        if not in_hunk:
            # The file header (diff --git, ---, +++) before the first hunk.
            # A patch with no hunk header counts its lines from 1.
            if line.startswith(("diff ", "index ", "--- ", "+++ ", "new file", "deleted file")):
                continue
            in_hunk, line_no = True, 1
        if line.startswith("+"):
            out.append((line_no, line[1:]))
            line_no += 1
        elif line.startswith(" ") or line == "":
            line_no += 1
    return out


def scan_text(path: str, patch: str) -> list[Finding]:
    """The findings in the added lines of one file's patch."""
    findings: list[Finding] = []
    for line_no, text in added_lines(patch):
        for name, pattern in PATTERNS:
            if pattern.search(text):
                findings.append(Finding(path=path, line_no=line_no, pattern_name=name))
    return findings


def scan_diff(diff: Diff) -> list[Finding]:
    """The findings in the added lines of every file in ``diff``."""
    findings: list[Finding] = []
    for item in diff.files:
        findings.extend(scan_text(item.path, item.patch))
    return findings
