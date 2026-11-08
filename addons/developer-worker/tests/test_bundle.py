"""The bundle of a failed push: saved on the manager, and restored by a resumed worker."""

from __future__ import annotations

from pathlib import Path

from conftest import (
    FakeManager,
    make_brief,
    remote_head,
    run_git,
)
from joshua_developer_worker import git, main
from test_main import BRANCH, result, run, write_file

SAVED = {
    "summary": "Added the search tool.",
    "files_changed": ["search.py"],
    "tests_run": ["pytest: 12 passed"],
}


def break_remote(tmp_path: Path):
    async def act(client) -> None:
        missing = tmp_path / "missing" / "remote.git"
        run_git("remote", "set-url", "origin", str(missing), cwd=Path(client.options.cwd))

    return act


def saved_bundle(tmp_path: Path, bare: Path, base: str, name: str = "search.py") -> bytes:
    """A bundle of one commit on BRANCH after ``base``, as a failed worker makes it."""
    work = tmp_path / "earlier-worker"
    run_git("clone", "--quiet", str(bare), str(work))
    run_git("checkout", "--quiet", "-b", BRANCH, f"origin/{base}", cwd=work)
    since = run_git("rev-parse", "HEAD", cwd=work).strip()
    (work / name).write_text("def search(): ...\n")
    run_git("add", name, cwd=work)
    run_git("commit", "--quiet", "-m", f"feat: add {name}", cwd=work)
    out = tmp_path / "earlier.bundle"
    git.bundle(work, since, BRANCH, out)
    return out.read_bytes()


async def test_a_failed_push_saves_a_bundle_of_the_commits(
    tmp_path: Path, home: Path, bare_repo: Path, manager: FakeManager
) -> None:
    manager.brief = make_brief(bare_repo)
    code, _ = await run(
        tmp_path,
        home,
        manager,
        [write_file("a.txt", commit=True), break_remote(tmp_path), result({"summary": "Done."})],
    )
    assert code == 0
    [report] = manager.reports
    assert report["status"] == "failed" and report["pushed"] is False
    assert report["unpushed_status"] == "success"
    assert report["bundle_saved"] is True
    assert report["error"].endswith(main.SAVED_NOTE)
    [data] = manager.bundles
    # The bundle holds the commit of the session, on top of main of the remote.
    check = tmp_path / "check"
    run_git("clone", "--quiet", str(bare_repo), str(check))
    path = tmp_path / "check.bundle"
    path.write_bytes(data)
    git.restore(check, path, BRANCH)
    assert run_git("log", "-1", "--format=%s", cwd=check).strip() == "feat: add a.txt"
    assert run_git("rev-parse", "HEAD~1", cwd=check).strip() == remote_head(bare_repo, "main")
    assert run_git("rev-parse", "HEAD", cwd=check).strip() == report["commit_hash"]


async def test_a_bundle_the_manager_refuses_says_the_work_is_lost(
    tmp_path: Path, home: Path, bare_repo: Path, manager: FakeManager
) -> None:
    manager.brief = make_brief(bare_repo)
    manager.bundle_status = 413
    await run(
        tmp_path,
        home,
        manager,
        [write_file("a.txt", commit=True), break_remote(tmp_path), result({"summary": "Done."})],
    )
    [report] = manager.reports
    assert report["bundle_saved"] is False
    assert report["error"].endswith(main.LOST_NOTE)
    assert "the bundle was not saved" in report["log"]


async def test_a_session_with_no_commits_saves_no_bundle(
    tmp_path: Path, home: Path, bare_repo: Path, manager: FakeManager
) -> None:
    manager.brief = make_brief(bare_repo)
    await run(tmp_path, home, manager, [result({"summary": "Nothing to do."})])
    [report] = manager.reports
    assert report["unpushed_status"] is None and report["bundle_saved"] is False
    assert manager.bundles == []


async def test_push_only_pushes_the_saved_commits_with_no_session(
    tmp_path: Path, home: Path, bare_repo: Path, manager: FakeManager
) -> None:
    main_head = remote_head(bare_repo, "main")
    manager.stored_bundle = saved_bundle(tmp_path, bare_repo, "main")
    manager.brief = make_brief(
        bare_repo, resumed=True, bundle=True, push_only=True, saved=SAVED, answer="push it"
    )
    code, made = await run(tmp_path, home, manager, [])
    assert code == 0
    assert made == []
    [report] = manager.reports
    assert report["status"] == "success" and report["pushed"] is True
    assert report["head"] == BRANCH
    assert report["summary"] == SAVED["summary"]
    assert report["files_changed"] == SAVED["files_changed"]
    assert report["tests_run"] == SAVED["tests_run"]
    # The branch was never pushed, so this worker made it; the scan starts at main.
    assert report["created_branch"] is True
    assert report["clone_head"] == main_head
    assert report["commit_hash"] == remote_head(bare_repo, BRANCH)
    msg = run_git("log", "-1", "--format=%s", BRANCH, cwd=bare_repo).strip()
    assert msg == "feat: add search.py"


async def test_push_only_that_fails_again_saves_the_bundle_again(
    tmp_path: Path, home: Path, bare_repo: Path, manager: FakeManager
) -> None:
    manager.stored_bundle = saved_bundle(tmp_path, bare_repo, "main")
    manager.brief = make_brief(bare_repo, resumed=True, bundle=True, push_only=True, saved=SAVED)
    # A pre-receive hook that refuses every push, as a host without write access does.
    hook = bare_repo / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho denied >&2\nexit 1\n")
    hook.chmod(0o755)
    code, made = await run(tmp_path, home, manager, [])
    assert code == 0 and made == []
    [report] = manager.reports
    assert report["status"] == "failed" and report["pushed"] is False
    assert report["unpushed_status"] == "success" and report["bundle_saved"] is True
    assert len(manager.bundles) == 1
    assert remote_head(bare_repo, BRANCH) is None


async def test_a_resumed_session_works_on_top_of_the_saved_commits(
    tmp_path: Path, home: Path, bare_repo: Path, manager: FakeManager
) -> None:
    manager.stored_bundle = saved_bundle(tmp_path, bare_repo, "main")
    manager.brief = make_brief(
        bare_repo, resumed=True, bundle=True, push_only=False, answer="Use port 8080."
    )
    code, made = await run(
        tmp_path, home, manager, [write_file("more.txt", commit=True), result({"summary": "Ok."})]
    )
    assert code == 0 and len(made) == 1
    [report] = manager.reports
    assert report["status"] == "success" and report["pushed"] is True
    log = run_git("log", "--format=%s", f"main..{BRANCH}", cwd=bare_repo).split("\n")
    assert log[:2] == ["feat: add more.txt", "feat: add search.py"]
    assert report["clone_head"] == remote_head(bare_repo, "main")


async def test_a_bundle_that_does_not_restore_fails_before_the_session(
    tmp_path: Path, home: Path, bare_repo: Path, manager: FakeManager
) -> None:
    manager.stored_bundle = b"# v2 git bundle\nnot really\n"
    manager.brief = make_brief(bare_repo, resumed=True, bundle=True, push_only=True, saved=SAVED)
    code, made = await run(tmp_path, home, manager, [])
    assert code == 0 and made == []
    [report] = manager.reports
    assert report["status"] == "failed"
    assert report["error"].startswith("the saved commits were not restored")
    assert manager.bundles == []


async def test_a_missing_bundle_fails_before_the_session(
    tmp_path: Path, home: Path, bare_repo: Path, manager: FakeManager
) -> None:
    manager.brief = make_brief(bare_repo, resumed=True, bundle=True, push_only=True, saved=SAVED)
    code, made = await run(tmp_path, home, manager, [])
    assert code == 0 and made == []
    [report] = manager.reports
    assert report["status"] == "failed"
    assert "no_bundle" in report["error"]
