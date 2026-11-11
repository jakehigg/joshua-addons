"""A worker continues in the checkout that an earlier worker of the task left in /work."""

from __future__ import annotations

import os
from pathlib import Path

from conftest import GIT_TOKEN, FakeManager, make_brief, remote_head, run_git
from joshua_developer_worker import git
from test_main import BRANCH, result, run, write_file


def _refuse_pushes(bare: Path, refuse: bool) -> None:
    hook = bare / "hooks" / "pre-receive"
    if refuse:
        hook.write_text("#!/bin/sh\necho 'push refused' >&2\nexit 1\n")
        hook.chmod(0o755)
    else:
        hook.unlink()


def _commit(cwd: Path, name: str) -> str:
    (cwd / name).write_text(f"{name}\n")
    run_git("add", name, cwd=cwd)
    run_git("commit", "-q", "-m", f"add {name}", cwd=cwd)
    return run_git("rev-parse", "HEAD", cwd=cwd).strip()


async def test_a_failed_push_keeps_the_commits_and_the_next_worker_pushes_them(
    tmp_path: Path, home: Path, bare_repo: Path
) -> None:
    main_head = remote_head(bare_repo, "main")
    first = FakeManager(brief=make_brief(bare_repo))
    _refuse_pushes(bare_repo, True)
    code, _ = await run(
        tmp_path, home, first, [write_file("a.txt", True), result({"summary": "A"})]
    )
    assert code == 0
    [report] = first.reports
    assert report["status"] == "failed" and report["pushed"] is False
    assert remote_head(bare_repo, BRANCH) is None
    checkout = tmp_path / "work" / "repo"
    kept = run_git("rev-parse", BRANCH, cwd=checkout).strip()
    assert run_git("log", "-1", "--format=%s", BRANCH, cwd=checkout).strip() == "feat: add a.txt"

    _refuse_pushes(bare_repo, False)
    second = FakeManager(brief=make_brief(bare_repo))
    code, made = await run(
        tmp_path, home, second, [write_file("b.txt", True), result({"summary": "B"})]
    )
    assert code == 0
    [report] = second.reports
    assert report["status"] == "success" and report["pushed"] is True
    # The diff to scan starts where the branch left main, so it holds both commits.
    assert report["clone_head"] == main_head
    assert report["created_branch"] is True
    assert run_git("rev-parse", f"{BRANCH}~1", cwd=bare_repo).strip() == kept
    assert sorted(report["files_changed"]) == ["a.txt", "b.txt"]
    assert "continued in the kept checkout" in report["log"]
    assert made, "the session ran in the kept checkout"


async def test_a_checkout_of_another_repository_is_cloned_fresh(
    tmp_path: Path, home: Path, bare_repo: Path
) -> None:
    other = tmp_path / "work" / "repo"
    other.mkdir(parents=True)
    run_git("init", "-q", "-b", "main", cwd=other)
    run_git("remote", "add", "origin", str(tmp_path / "elsewhere.git"), cwd=other)
    (other / "stale.txt").write_text("stale\n")
    manager = FakeManager(brief=make_brief(bare_repo))
    code, _ = await run(
        tmp_path, home, manager, [write_file("a.txt", True), result({"summary": "A"})]
    )
    assert code == 0
    [report] = manager.reports
    assert report["status"] == "success"
    assert report["clone_head"] == remote_head(bare_repo, "main")
    assert not (other / "stale.txt").exists()
    assert git.origin_url(other) == str(bare_repo)


def _kept(tmp_path: Path, bare: Path, branch: str) -> Path:
    """A kept checkout, with git configured as the worker configures it first."""
    home = Path(os.environ["HOME"])
    git.configure(home, "Alex Example", "alex@example.test", "git", GIT_TOKEN, "git.test")
    dest = tmp_path / "kept"
    run_git("clone", "-q", str(bare), str(dest))
    run_git("checkout", "-q", branch, cwd=dest)
    return dest


def test_a_local_branch_behind_the_remote_moves_to_the_remote(
    tmp_path: Path, bare_repo: Path
) -> None:
    dest = _kept(tmp_path, bare_repo, "feature")
    seed = tmp_path / "seed"
    run_git("checkout", "-q", "feature", cwd=seed)
    newer = _commit(seed, "newer.txt")
    run_git("push", "-q", "origin", "feature", cwd=seed)

    since, created = git.resume_checkout(dest, "feature", "main", create_if_missing=False)
    assert (since, created) == (newer, False)
    assert git.head(dest) == newer


def test_a_local_commit_is_rebased_on_a_remote_that_moved(tmp_path: Path, bare_repo: Path) -> None:
    dest = _kept(tmp_path, bare_repo, "feature")
    _commit(dest, "local.txt")
    seed = tmp_path / "seed"
    run_git("checkout", "-q", "feature", cwd=seed)
    remote_tip = _commit(seed, "remote.txt")
    run_git("push", "-q", "origin", "feature", cwd=seed)

    since, created = git.resume_checkout(dest, "feature", "main", create_if_missing=False)
    assert (since, created) == (remote_tip, False)
    assert run_git("rev-parse", "HEAD~1", cwd=dest).strip() == remote_tip
    assert (dest / "local.txt").exists() and (dest / "remote.txt").exists()
    assert git.has_commits_after(dest, since)


def test_a_rebase_conflict_keeps_the_local_branch(tmp_path: Path, bare_repo: Path) -> None:
    dest = _kept(tmp_path, bare_repo, "feature")
    local = _commit(dest, "same.txt")
    seed = tmp_path / "seed"
    run_git("checkout", "-q", "feature", cwd=seed)
    (seed / "same.txt").write_text("other\n")
    run_git("add", "same.txt", cwd=seed)
    run_git("commit", "-q", "-m", "conflict", cwd=seed)
    run_git("push", "-q", "origin", "feature", cwd=seed)

    since, _ = git.resume_checkout(dest, "feature", "main", create_if_missing=False)
    assert git.head(dest) == local
    assert since == run_git("rev-parse", "feature~1", cwd=bare_repo).strip()


def test_no_local_branch_is_the_same_as_a_fresh_checkout(tmp_path: Path, bare_repo: Path) -> None:
    dest = _kept(tmp_path, bare_repo, "main")
    since, created = git.resume_checkout(dest, "new-work", "main", create_if_missing=True)
    assert created is True
    assert since == remote_head(bare_repo, "main")
    assert git.has_commits_after(dest, since) is False
