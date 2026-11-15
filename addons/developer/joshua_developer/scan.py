"""The credential scan: the gate before a pull request.

After a worker pushes, the manager reads the diff over the platform API and
scans the added lines with ``gitleaks``. A finding names the file, the line
in the new file, and the gitleaks rule. It never holds the text that
matched, so a finding is safe to store, log, and send to a chat.

The scan writes the added lines of each file of the diff into a temporary
directory, one file for each file of the diff, and runs ``gitleaks dir``
over that directory. ``gitleaks.toml``, beside this module, is the
configuration: every default rule of gitleaks, and the allowlist of
placeholder values. The report names a line of the temporary file, and the
scan maps it back to the line of the new file.

Without the ``gitleaks`` binary, the scan raises ``ScanUnavailable``. It
never reports a diff clean that it did not read.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from joshua_developer.platforms import Diff

# The binary, and the configuration file that ships beside this module.
GITLEAKS = "gitleaks"
CONFIG_PATH = Path(__file__).with_name("gitleaks.toml")
# One scan is one call over the whole diff. The limit is for the largest
# diff a platform API sends.
TIMEOUT_S = 300
# The names in the temporary directory.
LINES_DIR = "lines"
REPORT_NAME = "report.json"

_HUNK_RE = re.compile(r"^@@ -[0-9]+(?:,[0-9]+)? \+([0-9]+)(?:,[0-9]+)? @@")


class ScanUnavailable(Exception):
    """gitleaks did not run, so the diff was not scanned.

    The message names no text of the diff.
    """


@dataclass(frozen=True)
class Finding:
    path: str
    line_no: int
    pattern_name: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class _ScannedFile:
    """One file of the diff, as it was written for gitleaks."""

    path: str
    written: str
    line_numbers: tuple[int, ...]


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


def available() -> bool:
    """True when the gitleaks binary is on the path and executable."""
    return shutil.which(GITLEAKS) is not None


def run_gitleaks(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run the gitleaks binary. A test puts its own function here.

    The output of gitleaks holds the text that matched, so no caller logs it.
    """
    return subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT_S, check=False)


def scan_text(path: str, patch: str) -> list[Finding]:
    """The findings in the added lines of one file's patch."""
    return _scan([(path, patch)])


def scan_diff(diff: Diff) -> list[Finding]:
    """The findings in the added lines of every file in ``diff``."""
    return _scan([(item.path, item.patch) for item in diff.files])


def _scan(files: list[tuple[str, str]]) -> list[Finding]:
    """The findings of gitleaks over the added lines of ``files``.

    The binary comes first: without it the scan is unavailable, also for a
    diff that adds no line.
    """
    if not available():
        raise ScanUnavailable(f"the {GITLEAKS} binary is not there")
    with tempfile.TemporaryDirectory(prefix="joshua-scan-") as name:
        root = Path(name)
        try:
            lines_dir = root / LINES_DIR
            lines_dir.mkdir()
            scanned = _write(lines_dir, files)
        except OSError as exc:
            raise ScanUnavailable(f"the scan wrote no files ({type(exc).__name__})") from None
        if not scanned:
            return []
        return _findings(_report(lines_dir, root / REPORT_NAME), scanned)


def _write(lines_dir: Path, files: list[tuple[str, str]]) -> list[_ScannedFile]:
    """Write the added lines of each file of the diff. Returns what was written.

    Each file goes into its own directory, named with the index of the file
    in the list, and keeps its own path in the repository, so a rule or an
    allowlist that reads the path still holds. A file that adds no line is
    not written.

    Every line ends with a newline, the last line also: gitleaks counts the
    line of a match in a file that ends with a newline right.
    """
    scanned: list[_ScannedFile] = []
    for path, patch in files:
        lines = added_lines(patch)
        if not lines:
            continue
        written = f"{len(scanned)}/{_relative(path)}"
        target = lines_dir / written
        target.parent.mkdir(parents=True, exist_ok=True)
        content = "".join(f"{text}\n" for _, text in lines)
        target.write_text(content, encoding="utf-8")
        scanned.append(
            _ScannedFile(path=path, written=written, line_numbers=tuple(no for no, _ in lines))
        )
    return scanned


def _relative(path: str) -> str:
    """The path of the copy in the temporary directory, under the index directory.

    A part that leaves the directory, and a leading separator, are dropped,
    so every copy stays in the directory that gitleaks reads.
    """
    parts = [part for part in PurePosixPath(path).parts if part not in ("/", ".", "..")]
    return "/".join(parts) or "file"


def _report(target: Path, report: Path) -> list[dict[str, Any]]:
    """The items of the JSON report of gitleaks over ``target``.

    ``--exit-code 0`` makes a run that finds a leak exit 0, so every other
    exit code is a failure of gitleaks itself, and the scan is unavailable.
    """
    args = [
        GITLEAKS,
        "dir",
        str(target),
        "--config",
        str(CONFIG_PATH),
        "--report-format",
        "json",
        "--report-path",
        str(report),
        "--exit-code",
        "0",
        "--no-banner",
    ]
    try:
        done = run_gitleaks(args)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ScanUnavailable(f"{GITLEAKS} did not run ({type(exc).__name__})") from None
    if done.returncode != 0:
        # The output of gitleaks holds the text that matched, so the message
        # names the exit code only.
        raise ScanUnavailable(f"{GITLEAKS} failed ({done.returncode})")
    try:
        text = report.read_text(encoding="utf-8").strip()
    except OSError:
        raise ScanUnavailable(f"{GITLEAKS} wrote no report") from None
    try:
        items = json.loads(text or "[]")
    except json.JSONDecodeError:
        raise ScanUnavailable(f"the {GITLEAKS} report is not JSON") from None
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ScanUnavailable(f"the {GITLEAKS} report is not a list of findings")
    return items


def _findings(items: list[dict[str, Any]], scanned: list[_ScannedFile]) -> list[Finding]:
    """The findings of the report, with the path and the line of the diff.

    Only the rule, the file, and the line of an item are kept. ``Secret``
    and ``Match`` are dropped here, and go into no log and no report.
    """
    keys: set[tuple[int, int, str]] = set()
    for item in items:
        index = _index(str(item.get("File", "")), scanned)
        if index is None:
            # A finding the scan cannot place is a failure of the scan,
            # never a clean diff.
            raise ScanUnavailable(f"the {GITLEAKS} report names a file outside the scan")
        rule = str(item.get("RuleID") or "unknown-rule")
        keys.add((index, _line_no(scanned[index].line_numbers, item.get("StartLine")), rule))
    return [
        Finding(path=scanned[index].path, line_no=line_no, pattern_name=rule)
        for index, line_no, rule in sorted(keys)
    ]


def _index(file: str, scanned: list[_ScannedFile]) -> int | None:
    """The index of the file of the diff, from the path in the report.

    The report names the copy in the temporary directory. The index
    directory makes the end of that path unique for each file of the diff.
    """
    found = PurePosixPath(file).as_posix()
    for index, item in enumerate(scanned):
        if found == item.written or found.endswith(f"/{item.written}"):
            return index
    return None


def _line_no(line_numbers: tuple[int, ...], start_line: object) -> int:
    """The line of the new file for ``start_line`` of the temporary file.

    gitleaks counts the lines of a file from 1. A line outside the file
    takes the nearest line, so a finding always names a line of the diff.
    """
    index = start_line - 1 if isinstance(start_line, int) else 0
    return line_numbers[min(max(index, 0), len(line_numbers) - 1)]
