"""Repository names, pull request references, and branch names.

A repository is ``host/owner/name`` in lowercase. ``owner`` can hold more than
one part, for a GitLab subgroup. A caller can give a URL (``https://``,
``ssh://``, or ``git@host:owner/name``) or the short form; both become the
short form. An error message never repeats the input, because a URL can hold
a credential.
"""

from __future__ import annotations

import fnmatch
import re
import unicodedata
from collections.abc import Iterable
from urllib.parse import urlsplit

_PART_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*\Z")
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?\Z")
_SCP_RE = re.compile(r"^[^/@:]+@([^/:]+):(.+)\Z")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}\Z")
_PR_URL_RE = re.compile(r"/(?:pull|pulls|merge_requests)/([0-9]+)/?\Z")


class RepoError(ValueError):
    """A repository, a pull request, or a branch is not valid."""


def normalize_repo(raw: str) -> str:
    """Return ``raw`` as ``host/owner/name``. Raises RepoError when it is not a repository."""
    text = (raw or "").strip()
    bad = RepoError("repo must be a URL or host/owner/name, such as github.com/owner/name")
    if not text:
        raise bad
    scp = _SCP_RE.match(text)
    if "://" in text:
        parts = urlsplit(text)
        if parts.scheme not in ("http", "https", "ssh", "git"):
            raise bad
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        path = parts.path
    elif scp:
        host, path = scp.group(1), scp.group(2)
    else:
        host, _, path = text.partition("/")
    host = host.lower()
    path = path.strip("/").lower()
    if path.endswith(".git"):
        path = path[: -len(".git")]
    segments = path.split("/") if path else []
    if not _HOST_RE.match(host) or "." not in host.split(":")[0] or len(segments) < 2:
        raise bad
    if any(not _PART_RE.match(s) or s in (".", "..") for s in segments):
        raise bad
    return "/".join([host, *segments])


def repo_host(repo: str) -> str:
    """The host of a normalized repository."""
    return repo.split("/", 1)[0]


def repo_allowed(repo: str, patterns: Iterable[str]) -> bool:
    """True when ``repo`` matches one of ``patterns`` (fnmatch, lowercase)."""
    return any(fnmatch.fnmatchcase(repo, pattern.strip().lower()) for pattern in patterns)


def parse_pr(pr: int | str) -> tuple[int, str | None]:
    """Return the pull request number and, when ``pr`` is a URL, the URL."""
    if isinstance(pr, int) and not isinstance(pr, bool):
        number, url = pr, None
    else:
        text = str(pr).strip().lstrip("#")
        if text.isdigit():
            number, url = int(text), None
        else:
            match = _PR_URL_RE.search(urlsplit(text).path) if "://" in text else None
            if match is None:
                raise RepoError(
                    "pr must be a number or a pull request URL "
                    "(.../pull/<n> or .../merge_requests/<n>)"
                )
            number, url = int(match.group(1)), text
    if number <= 0:
        raise RepoError("pr must be a positive number")
    return number, url


def check_branch(name: str) -> str:
    """Return ``name`` when it is a safe branch name. Raises RepoError when it is not."""
    text = (name or "").strip()
    if (
        not _BRANCH_RE.match(text)
        or ".." in text
        or "//" in text
        or text.endswith(("/", ".", ".lock"))
    ):
        raise RepoError("branch is not a valid branch name")
    return text


def brief_title(brief: str, limit: int = 72) -> str:
    """The first line of ``brief`` that has text, cut to ``limit`` characters."""
    for line in (brief or "").splitlines():
        text = " ".join(line.split())
        if text:
            return text[:limit].rstrip()
    return ""


def slugify(text: str, limit: int = 40) -> str:
    """``text`` in kebab case, ASCII only, at most ``limit`` characters."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:limit].rstrip("-")


def default_branch_name(task_id: str, brief: str = "") -> str:
    """The branch a ``develop`` task gets when the caller names none.

    ``joshua/<slug>-<id6>``: the slug is the first line of the brief in kebab
    case, and ``id6`` is the start of the task id.
    """
    slug = slugify(brief_title(brief, limit=200)) or "task"
    return f"joshua/{slug}-{task_id.replace('-', '')[:6]}"
