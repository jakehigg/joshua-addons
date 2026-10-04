"""Every git call of the worker. Each one is a local subprocess.

The git token lives in ``$HOME/.git-credentials`` (mode 0600), read by the
``store`` credential helper. The tunnel of the manager is set only in git's
own config (``http.proxy``), never as ``HTTPS_PROXY`` in the environment, so
the Claude CLI does not send its requests through the tunnel.

No error string from this module holds a secret: ``scrub`` removes the
credentials of every URL, each registered secret, and the path of the
credentials file.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

from joshua_developer_worker.log import get_logger

logger = get_logger("joshua_developer_worker.git")

CREDENTIALS_FILE = ".git-credentials"
# The worker's push runs no hook from the checkout.
NO_HOOKS = ("-c", "core.hooksPath=/dev/null")
TIMEOUT_S = 600

_URL_CREDENTIALS = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@")
_secrets: set[str] = set()


class GitError(Exception):
    """A git command failed. The message is scrubbed."""


def register_secret(value: str | None) -> None:
    """Add ``value`` to the strings that ``scrub`` removes."""
    if value:
        _secrets.add(value)


def scrub(text: str) -> str:
    """Remove URL credentials, registered secrets, and the credentials file path."""
    out = _URL_CREDENTIALS.sub(r"\1***@", text)
    for secret in sorted(_secrets, key=len, reverse=True):
        out = out.replace(secret, "***")
        quoted = quote(secret, safe="")
        if quoted != secret:
            out = out.replace(quoted, "***")
    return re.sub(r"\S*" + re.escape(CREDENTIALS_FILE), "<credentials>", out)


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def _run(args: list[str], cwd: Path | None = None) -> str:
    """Run ``git <args>``. Returns stdout. Raises GitError with a scrubbed message."""
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=_env(),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(scrub(f"git {args[0]} did not run: {exc}")) from None
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip()[-2000:]
        raise GitError(scrub(f"git {args[0]} failed ({done.returncode}): {detail}"))
    return done.stdout


def configure(
    home: Path,
    name: str,
    email: str,
    username: str,
    token: str,
    host: str,
    proxy_url: str | None = None,
) -> None:
    """Write the credentials file and the global git config under ``home``.

    ``home`` must be ``$HOME`` of this process, because git reads its global
    config and the credentials file from there. ``proxy_url`` goes into
    ``http.proxy``, and into no environment variable.
    """
    register_secret(token)
    register_secret(proxy_url)
    home.mkdir(parents=True, exist_ok=True)
    path = home / CREDENTIALS_FILE
    line = f"https://{quote(username, safe='')}:{quote(token, safe='')}@{host}\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(line)
    path.chmod(0o600)
    _run(["config", "--global", "credential.helper", "store"])
    _run(["config", "--global", "user.name", name])
    _run(["config", "--global", "user.email", email])
    _run(["config", "--global", "init.defaultBranch", "main"])
    if proxy_url:
        _run(["config", "--global", "http.proxy", proxy_url])
        # curl sends a Basic proxy credential at once with this; with the
        # default it waits for a 407 first.
        _run(["config", "--global", "http.proxyAuthMethod", "basic"])


def clone(repo_url: str, dest: Path) -> None:
    """Clone ``repo_url`` into ``dest``."""
    _run(["clone", "--quiet", repo_url, str(dest)])


def _remote_has_branch(dest: Path, branch: str) -> bool:
    out = _run(["ls-remote", "--heads", "origin", branch], cwd=dest)
    return any(line.endswith(f"refs/heads/{branch}") for line in out.splitlines())


def checkout(
    dest: Path, branch: str, base_branch: str | None, create_if_missing: bool
) -> tuple[str, bool]:
    """Check out ``branch``. Returns the head commit, and True when the branch is new.

    When the remote has the branch, the checkout tracks it. When it has not
    and ``create_if_missing`` is set, the branch starts at ``base_branch`` of
    the remote, or at the default branch when ``base_branch`` is None.
    """
    created = not _remote_has_branch(dest, branch)
    if not created:
        _run(["fetch", "--quiet", "origin", branch], cwd=dest)
        _run(["checkout", "--quiet", "-B", branch, f"origin/{branch}"], cwd=dest)
    elif not create_if_missing:
        raise GitError(f"the remote has no branch {branch}")
    elif base_branch:
        if not _remote_has_branch(dest, base_branch):
            raise GitError(f"the remote has no base branch {base_branch}")
        _run(["checkout", "--quiet", "-b", branch, f"origin/{base_branch}"], cwd=dest)
    else:
        _run(["checkout", "--quiet", "-b", branch], cwd=dest)
    commit = head(dest)
    if commit is None:
        raise GitError("the checkout has no commit")
    return commit, created


def head(dest: Path) -> str | None:
    """The head commit, or None when the branch has no commit."""
    try:
        return _run(["rev-parse", "--verify", "--quiet", "HEAD"], cwd=dest).strip() or None
    except GitError:
        return None


def dirty(dest: Path) -> bool:
    """True when the work tree or the index has a change, untracked files included."""
    return bool(uncommitted(dest))


def uncommitted(dest: Path) -> list[str]:
    """The paths with a change the model did not commit, untracked files included.

    The worker never commits them. They are named in the report and are lost
    with the container: a commit the worker made itself could carry a file
    the model never meant to publish.
    """
    out = _run(["status", "--porcelain", "--untracked-files=all"], cwd=dest)
    return [line[3:] for line in out.splitlines() if line.strip()]


def changed_files(dest: Path, since: str) -> list[str]:
    """The paths that changed between ``since`` and HEAD."""
    out = _run(["diff", "--name-only", "-z", since, "HEAD"], cwd=dest)
    return [name for name in out.split("\0") if name]


def push(dest: Path, branch: str) -> tuple[bool, str | None]:
    """Push ``branch`` to origin. Returns (True, None), or (False, a scrubbed error)."""
    try:
        _run([*NO_HOOKS, "push", "--quiet", "--no-verify", "-u", "origin", branch], cwd=dest)
    except GitError as exc:
        return False, str(exc)
    return True, None
