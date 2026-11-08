"""Tests for the git calls, against a local bare repository."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
from conftest import GIT_TOKEN, PROXY_URL, TASK_TOKEN, remote_head, run_git
from joshua_developer_worker import git


def _configure(home: Path) -> None:
    git.configure(
        home,
        "Alex Example",
        "alex@example.test",
        "x-access-token",
        GIT_TOKEN,
        "git.test",
        PROXY_URL,
    )


def _global(key: str) -> str:
    done = subprocess.run(
        ["git", "config", "--global", "--get", key], capture_output=True, text=True
    )
    return done.stdout.strip()


def test_configure_writes_the_credentials_at_0600(home: Path) -> None:
    _configure(home)
    path = home / ".git-credentials"
    assert path.read_text() == f"https://x-access-token:{GIT_TOKEN}@git.test\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert _global("credential.helper") == "store"
    assert _global("user.name") == "Alex Example"
    assert _global("user.email") == "alex@example.test"


def test_the_proxy_is_in_git_config_and_not_in_the_environment(home: Path) -> None:
    _configure(home)
    assert _global("http.proxy") == PROXY_URL
    assert _global("http.proxyAuthMethod") == "basic"
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        assert name not in os.environ


def test_configure_without_a_proxy_sets_none(home: Path) -> None:
    git.configure(home, "A", "a@example.test", "git", "t0k", "git.test")
    assert _global("http.proxy") == ""
    assert _global("http.proxyAuthMethod") == ""


def test_a_ca_bundle_goes_under_home_and_into_ssl_ca_info(home: Path) -> None:
    pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
    git.configure(home, "A", "a@example.test", "git", "t0k", "git.test", ca_pem=pem)
    path = home / git.CA_FILE
    assert path.read_text() == pem
    assert _global("http.sslCAInfo") == str(path)


def test_no_ca_bundle_sets_no_ssl_ca_info(home: Path) -> None:
    _configure(home)
    assert _global("http.sslCAInfo") == ""
    assert not (home / git.CA_FILE).exists()


def test_scrub_removes_the_token_the_url_and_the_credentials_path(home: Path) -> None:
    _configure(home)
    raw = (
        f"fatal: unable to access 'https://x-access-token:{GIT_TOKEN}@git.test/o/r.git/': "
        f"proxy http://task:{TASK_TOKEN}@manager.test:8002 refused; "
        f"token {GIT_TOKEN} in {home}/.git-credentials"
    )
    clean = git.scrub(raw)
    assert GIT_TOKEN not in clean
    assert TASK_TOKEN not in clean
    assert ".git-credentials" not in clean
    assert "https://***@git.test/o/r.git/" in clean
    assert "<credentials>" in clean


def test_scrub_removes_a_url_encoded_secret() -> None:
    git.register_secret("a b/c")
    assert "a%20b%2Fc" not in git.scrub("x a%20b%2Fc y")
    git.register_secret(None)


def test_a_failed_command_never_shows_the_token(home: Path, tmp_path: Path) -> None:
    _configure(home)
    with pytest.raises(git.GitError) as info:
        # A local path that holds the token: git prints it, and no socket opens.
        git.clone(str(tmp_path / "missing" / GIT_TOKEN / "r.git"), tmp_path / "x")
    assert GIT_TOKEN not in str(info.value)
    assert "clone failed" in str(info.value)


def test_a_command_that_cannot_start_is_a_git_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise OSError(f"no git for {GIT_TOKEN}")

    git.register_secret(GIT_TOKEN)
    monkeypatch.setattr(git.subprocess, "run", broken)
    with pytest.raises(git.GitError) as info:
        git.dirty(Path("."))
    assert GIT_TOKEN not in str(info.value)


def test_develop_creates_the_branch_from_the_base(
    home: Path, bare_repo: Path, tmp_path: Path
) -> None:
    _configure(home)
    dest = tmp_path / "work"
    git.clone(str(bare_repo), dest)
    commit, created = git.checkout(dest, "developer/new", "main", create_if_missing=True)
    assert commit == remote_head(bare_repo, "main")
    assert created is True
    assert run_git("branch", "--show-current", cwd=dest).strip() == "developer/new"


def test_develop_without_a_base_starts_at_the_default_branch(
    home: Path, bare_repo: Path, tmp_path: Path
) -> None:
    _configure(home)
    dest = tmp_path / "work"
    git.clone(str(bare_repo), dest)
    assert git.checkout(dest, "developer/new", None, True) == (remote_head(bare_repo, "main"), True)


def test_a_missing_base_branch_is_an_error(home: Path, bare_repo: Path, tmp_path: Path) -> None:
    _configure(home)
    dest = tmp_path / "work"
    git.clone(str(bare_repo), dest)
    with pytest.raises(git.GitError, match="no base branch"):
        git.checkout(dest, "developer/new", "nope", True)


def test_rework_checks_out_the_existing_branch(home: Path, bare_repo: Path, tmp_path: Path) -> None:
    _configure(home)
    dest = tmp_path / "work"
    git.clone(str(bare_repo), dest)
    assert git.checkout(dest, "feature", "main", False) == (
        remote_head(bare_repo, "feature"),
        False,
    )
    assert (dest / "feature.txt").is_file()


def test_rework_refuses_a_missing_branch(home: Path, bare_repo: Path, tmp_path: Path) -> None:
    _configure(home)
    dest = tmp_path / "work"
    git.clone(str(bare_repo), dest)
    with pytest.raises(git.GitError, match="no branch"):
        git.checkout(dest, "gone", "main", False)


def test_dirty_uncommitted_and_push(home: Path, bare_repo: Path, tmp_path: Path) -> None:
    _configure(home)
    dest = tmp_path / "work"
    git.clone(str(bare_repo), dest)
    start, _ = git.checkout(dest, "developer/new", "main", True)
    assert git.dirty(dest) is False
    assert git.uncommitted(dest) == []
    (dest / "hello.txt").write_text("hi\n")
    (dest / "sub").mkdir()
    (dest / "sub" / "two.txt").write_text("2\n")
    assert git.dirty(dest) is True
    assert sorted(git.uncommitted(dest)) == ["hello.txt", "sub/two.txt"]
    run_git("add", "hello.txt", cwd=dest)
    # The test helper sets its own seed identity; the model's commits carry
    # the person's, which the worker put in the global config.
    run_git(
        "-c",
        "user.name=Alex Example",
        "-c",
        "user.email=alex@example.test",
        "commit",
        "-q",
        "-m",
        "feat: add hello",
        cwd=dest,
    )
    new = git.head(dest)
    assert new and new != start
    assert git.uncommitted(dest) == ["sub/two.txt"]
    assert git.changed_files(dest, start) == ["hello.txt"]
    author = run_git("log", "-1", "--format=%an <%ae>", cwd=dest).strip()
    assert author == "Alex Example <alex@example.test>"
    assert git.push(dest, "developer/new") == (True, None)
    assert remote_head(bare_repo, "developer/new") == new


def test_push_runs_no_hook(home: Path, bare_repo: Path, tmp_path: Path) -> None:
    _configure(home)
    dest = tmp_path / "work"
    git.clone(str(bare_repo), dest)
    git.checkout(dest, "developer/new", "main", True)
    hook = dest / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    (dest / "a.txt").write_text("a\n")
    run_git("add", "a.txt", cwd=dest)
    run_git("commit", "-q", "-m", "feat: a", cwd=dest)
    assert git.push(dest, "developer/new") == (True, None)


def test_a_failed_push_returns_a_scrubbed_error(
    home: Path, bare_repo: Path, tmp_path: Path
) -> None:
    _configure(home)
    dest = tmp_path / "work"
    git.clone(str(bare_repo), dest)
    git.checkout(dest, "developer/new", "main", True)
    (dest / "a.txt").write_text("a\n")
    run_git("add", "a.txt", cwd=dest)
    run_git("commit", "-q", "-m", "feat: a", cwd=dest)
    run_git("remote", "set-url", "origin", str(tmp_path / "missing" / GIT_TOKEN), cwd=dest)
    ok, error = git.push(dest, "developer/new")
    assert ok is False
    assert error and GIT_TOKEN not in error


def test_head_of_an_empty_repository_is_none(tmp_path: Path) -> None:
    run_git("init", "--quiet", str(tmp_path / "empty"))
    assert git.head(tmp_path / "empty") is None
