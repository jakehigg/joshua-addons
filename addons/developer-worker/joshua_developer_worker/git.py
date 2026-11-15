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

import contextlib
import os
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from joshua_developer_worker.log import get_logger

logger = get_logger("joshua_developer_worker.git")

CREDENTIALS_FILE = ".git-credentials"
# The CA bundle of the git host, from the brief, when the manager has one.
CA_FILE = "git-ca.pem"
# The worker's push, and its git calls in a kept checkout, run no hook from the checkout.
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
    ca_pem: str | None = None,
    extra_credentials: list[tuple[str, str, str]] | None = None,
) -> None:
    """Write the credentials file and the global git config under ``home``.

    ``extra_credentials`` holds ``(username, token, host)`` for each other
    host, the hosts of the reference repositories.

    ``home`` must be ``$HOME`` of this process, because git reads its global
    config and the credentials file from there. ``proxy_url`` goes into
    ``http.proxy``, and into no environment variable. ``ca_pem`` is a CA
    bundle in PEM: git then trusts it for the git host (``http.sslCAInfo``).
    """
    register_secret(token)
    register_secret(proxy_url)
    home.mkdir(parents=True, exist_ok=True)
    path = home / CREDENTIALS_FILE
    entries = [(username, token, host), *(extra_credentials or [])]
    lines = []
    for entry_user, entry_token, entry_host in entries:
        register_secret(entry_token)
        lines.append(
            f"https://{quote(entry_user, safe='')}:{quote(entry_token, safe='')}@{entry_host}\n"
        )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write("".join(lines))
    path.chmod(0o600)
    _run(["config", "--global", "credential.helper", "store"])
    _run(["config", "--global", "user.name", name])
    _run(["config", "--global", "user.email", email])
    _run(["config", "--global", "init.defaultBranch", "main"])
    if ca_pem:
        ca_path = home / CA_FILE
        ca_path.write_text(ca_pem, encoding="utf-8")
        _run(["config", "--global", "http.sslCAInfo", str(ca_path)])
    if proxy_url:
        _run(["config", "--global", "http.proxy", proxy_url])
        # curl sends a Basic proxy credential at once with this; with the
        # default it waits for a 407 first.
        _run(["config", "--global", "http.proxyAuthMethod", "basic"])


def clone(repo_url: str, dest: Path) -> None:
    """Clone ``repo_url`` into ``dest``."""
    _run(["clone", "--quiet", repo_url, str(dest)])


# The push URL of a reference clone: no transport, so every push fails.
NO_PUSH_URL = "no-push://reference-repository-is-read-only"


def clone_reference(repo_url: str, dest: Path) -> None:
    """Clone ``repo_url`` into ``dest`` for reading only.

    The clone is shallow, on the default branch. Its push URL goes nowhere,
    and every file and folder in it is read-only. This is a guard, not a
    boundary: the token can still write to what the host lets it write.
    """
    _run(["clone", "--quiet", "--depth", "1", "--single-branch", repo_url, str(dest)])
    _run(["remote", "set-url", "--push", "origin", NO_PUSH_URL], cwd=dest)
    set_read_only(dest, True)


def set_read_only(root: Path, read_only: bool) -> None:
    """Remove (``read_only``) or give back the write bits of every path under ``root``."""
    paths = [root, *root.rglob("*")]
    for path in paths:
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        path.chmod(mode & ~0o222 if read_only else mode | 0o200)


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


def origin_url(dest: Path) -> str | None:
    """The URL of ``origin`` in the checkout at ``dest``, or None when it has none."""
    try:
        return _run(["remote", "get-url", "origin"], cwd=dest).strip() or None
    except GitError:
        return None


def _has_ref(dest: Path, ref: str) -> bool:
    try:
        _run(["rev-parse", "--verify", "--quiet", ref], cwd=dest)
    except GitError:
        return False
    return True


def _is_ancestor(dest: Path, older: str, newer: str) -> bool:
    try:
        _run(["merge-base", "--is-ancestor", older, newer], cwd=dest)
    except GitError:
        return False
    return True


def resume_checkout(
    dest: Path, branch: str, base_branch: str | None, create_if_missing: bool
) -> tuple[str, bool]:
    """Continue on ``branch`` in a checkout that an earlier worker left at ``dest``.

    Returns the commit the remote has for the work (the remote branch, or the
    point where the branch left the base), and True when the remote has no
    branch. The commits of the earlier worker after that commit stay, so the
    push sends them and the scan reads them.

    - A local branch that the remote branch is ahead of moves to the remote.
    - A local branch with commits that the remote does not have stays, and
      is rebased on the remote branch when the two moved apart. When the
      rebase fails, the local branch stays as it was.
    - With no local branch, it is the same as ``checkout``.
    """
    stale = dest / ".git" / "index.lock"
    stale.unlink(missing_ok=True)
    _run(["fetch", "--quiet", "--prune", "origin"], cwd=dest)
    remote = _remote_has_branch(dest, branch)
    remote_ref = f"refs/remotes/origin/{branch}"
    if not _has_ref(dest, f"refs/heads/{branch}"):
        return checkout(dest, branch, base_branch, create_if_missing)
    # The kept checkout is written by a worker, so its hooks never run here.
    _run([*NO_HOOKS, "checkout", "--quiet", branch], cwd=dest)
    if remote and _is_ancestor(dest, "HEAD", remote_ref):
        _run([*NO_HOOKS, "reset", "--quiet", "--keep", remote_ref], cwd=dest)
    elif remote and not _is_ancestor(dest, remote_ref, "HEAD"):
        try:
            _run([*NO_HOOKS, "rebase", "--quiet", remote_ref], cwd=dest)
        except GitError:
            with contextlib.suppress(GitError):
                _run([*NO_HOOKS, "rebase", "--abort"], cwd=dest)
    if remote:
        since = _run(["merge-base", "HEAD", remote_ref], cwd=dest).strip()
    else:
        base_ref = (
            f"refs/remotes/origin/{base_branch}" if base_branch else "refs/remotes/origin/HEAD"
        )
        if not _has_ref(dest, base_ref):
            raise GitError(f"the remote has no base branch {base_branch or 'HEAD'}")
        since = _run(["merge-base", "HEAD", base_ref], cwd=dest).strip()
    if not since:
        raise GitError("the kept checkout has no commit in common with the remote")
    return since, not remote


def has_commits_after(dest: Path, since: str) -> bool:
    """True when HEAD has a commit after ``since``."""
    try:
        out = _run(["rev-list", "--count", f"{since}..HEAD"], cwd=dest)
    except GitError:
        return False
    return int(out.strip() or "0") > 0


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


# The git errors of a tunnel that is not there, such as a manager that restarts.
_TUNNEL_DOWN = (
    "failed to connect",
    "couldn't connect",
    "could not connect",
    "connection refused",
    "connection reset",
    "could not resolve proxy",
    "proxy connect aborted",
    "timed out",
    "empty reply from server",
)
PUSH_BACKOFF_S = 2.0
PUSH_MAX_BACKOFF_S = 15.0


def tunnel_down(error: str) -> bool:
    """True when a git error says the tunnel of the manager did not answer."""
    lowered = error.lower()
    return any(marker in lowered for marker in _TUNNEL_DOWN)


def push(
    dest: Path,
    branch: str,
    retry_s: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, str | None]:
    """Push ``branch`` to origin. Returns (True, None), or (False, a scrubbed error).

    A push that fails because the tunnel did not answer is tried again, with
    a longer wait each time, for ``retry_s`` seconds. Any other failure, such
    as a refused credential, returns at once.
    """
    give_up = time.monotonic() + retry_s
    attempt = 0
    while True:
        try:
            _run([*NO_HOOKS, "push", "--quiet", "--no-verify", "-u", "origin", branch], cwd=dest)
        except GitError as exc:
            error = str(exc)
            now = time.monotonic()
            if not tunnel_down(error) or now >= give_up:
                return False, error
            attempt += 1
            logger.warning({"message": "the push did not reach the tunnel; trying again"})
            delay = min(PUSH_BACKOFF_S * 2 ** min(attempt - 1, 10), PUSH_MAX_BACKOFF_S)
            sleep(min(delay, give_up - now))
            continue
        return True, None


def bundle(dest: Path, since: str, branch: str, out: Path) -> None:
    """Write the commits of ``branch`` after ``since`` to the bundle file ``out``."""
    _run(["bundle", "create", "--quiet", str(out), f"{since}..{branch}"], cwd=dest)


def restore(dest: Path, path: Path, branch: str) -> str:
    """Check out ``branch`` from the bundle at ``path``. Returns the new head commit.

    The bundle must hold ``refs/heads/<branch>``, and the checkout must hold
    the commit that the bundle starts from.
    """
    _run(["bundle", "verify", "--quiet", str(path)], cwd=dest)
    _run(
        ["fetch", "--quiet", str(path), f"+refs/heads/{branch}:refs/developer/saved"],
        cwd=dest,
    )
    _run(["checkout", "--quiet", "-B", branch, "refs/developer/saved"], cwd=dest)
    commit = head(dest)
    if commit is None:
        raise GitError("the bundle has no commit")
    return commit
