"""Reference repositories: cloned read-only next to the checkout, read by the session,
never pushed (#75)."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest
from conftest import GIT_TOKEN, FakeManager, FakeSDKClient, make_brief, remote_head, run_git
from joshua_developer_worker import git, main, prompt
from test_main import BRANCH, result, run

CHAT = "git.test/owner/chat"
CHAT_TOKEN = "chat-host-token-SECRET-654"


@pytest.fixture
def chat_repo(tmp_path: Path) -> Path:
    """A bare repository with the source to port: chat/Markdown.tsx on main."""
    bare = tmp_path / "chat.git"
    run_git("init", "--quiet", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "chat-seed"
    run_git("clone", "--quiet", str(bare), str(seed))
    (seed / "chat").mkdir()
    (seed / "chat" / "Markdown.tsx").write_text("export const Markdown = () => null;\n")
    run_git("add", ".", cwd=seed)
    run_git("commit", "--quiet", "-m", "chat", cwd=seed)
    run_git("push", "--quiet", "origin", "HEAD:main", cwd=seed)
    return bare


def ref_brief(bare_repo: Path, chat_repo: Path, **extra: Any) -> dict[str, Any]:
    return make_brief(
        bare_repo,
        refs=[{"repo": CHAT, "repo_url": str(chat_repo)}],
        ref_credentials=[{"host": "chat.test", "username": "oauth2", "token": CHAT_TOKEN}],
        **extra,
    )


def port_markdown(reference: dict[str, Path]):
    """A script step: the model copies the reference file into the checkout and commits."""

    async def act(client: FakeSDKClient) -> None:
        cwd = Path(client.options.cwd)
        source = Path(client.options.add_dirs[0]) / "chat" / "Markdown.tsx"
        reference["dir"] = Path(client.options.add_dirs[0])
        (cwd / "Markdown.tsx").write_text(source.read_text())
        run_git("add", "Markdown.tsx", cwd=cwd)
        run_git(
            "-c",
            "user.name=A",
            "-c",
            "user.email=a@example.test",
            "commit",
            "-q",
            "-m",
            "port Markdown",
            cwd=cwd,
        )

    return act


async def test_the_worker_ports_from_a_reference_and_pushes_only_the_task_repo(
    tmp_path: Path, home: Path, bare_repo: Path, chat_repo: Path
) -> None:
    chat_main = remote_head(chat_repo, "main")
    manager = FakeManager(brief=ref_brief(bare_repo, chat_repo))
    seen: dict[str, Path] = {}
    code, made = await run(
        tmp_path, home, manager, [port_markdown(seen), result({"summary": "Ported."})]
    )
    assert code == 0
    [report] = manager.reports
    assert report["status"] == "success" and report["pushed"] is True
    ported = run_git("show", f"{BRANCH}:Markdown.tsx", cwd=bare_repo)
    assert ported == "export const Markdown = () => null;\n"
    # The reference repository got no new branch and no new commit.
    assert remote_head(chat_repo, BRANCH) is None
    assert remote_head(chat_repo, "main") == chat_main
    ref_dir = tmp_path / "work" / "refs" / "git.test" / "owner" / "chat"
    assert seen["dir"] == ref_dir
    assert made[0].options.add_dirs == [str(ref_dir)]
    system = made[0].options.system_prompt
    assert "## Reference repositories" in system and f"- {CHAT}: `{ref_dir}`" in system
    assert "Never change, commit, or push in a reference repository" in system
    assert "cloned the reference repository git.test/owner/chat read-only" in report["log"]


def test_a_reference_clone_is_read_only_and_cannot_push(tmp_path: Path, chat_repo: Path) -> None:
    dest = tmp_path / "ref"
    git.clone_reference(str(chat_repo), dest)
    assert run_git("remote", "get-url", "--push", "origin", cwd=dest).strip() == git.NO_PUSH_URL
    for path in (dest, dest / "chat", dest / "chat" / "Markdown.tsx"):
        assert not stat.S_IMODE(path.stat().st_mode) & 0o222, path
    # A shallow clone: one commit.
    assert run_git("rev-list", "--count", "HEAD", cwd=dest).strip() == "1"
    if os.geteuid() != 0:
        with pytest.raises(PermissionError):
            (dest / "chat" / "new.txt").write_text("x")
    git.set_read_only(dest, False)
    (dest / "chat" / "new.txt").write_text("x")


async def test_the_credential_of_each_reference_host_is_written(
    tmp_path: Path, home: Path, bare_repo: Path, chat_repo: Path
) -> None:
    manager = FakeManager(brief=ref_brief(bare_repo, chat_repo))
    await run(tmp_path, home, manager, [result({"summary": "Nothing."})])
    lines = (home / ".git-credentials").read_text().splitlines()
    assert len(lines) == 2
    assert "x-access-token" in lines[0] and GIT_TOKEN in lines[0]
    assert lines[1] == f"https://oauth2:{CHAT_TOKEN}@chat.test"
    [report] = manager.reports
    assert CHAT_TOKEN not in report["log"]


async def test_a_reference_that_cannot_be_cloned_fails_before_the_session(
    tmp_path: Path, home: Path, bare_repo: Path
) -> None:
    brief = make_brief(bare_repo, refs=[{"repo": CHAT, "repo_url": str(tmp_path / "missing.git")}])
    manager = FakeManager(brief=brief)
    code, made = await run(tmp_path, home, manager, [])
    assert code == 0 and made == []
    [report] = manager.reports
    assert report["status"] == "failed"
    assert f"the reference repository {CHAT} was not cloned" in report["error"]


async def test_a_kept_work_directory_gets_fresh_reference_clones(
    tmp_path: Path, home: Path, bare_repo: Path, chat_repo: Path
) -> None:
    stale = tmp_path / "work" / "refs" / "git.test" / "owner" / "chat"
    stale.mkdir(parents=True)
    (stale / "stale.txt").write_text("old\n")
    git.set_read_only(tmp_path / "work" / "refs", True)
    manager = FakeManager(brief=ref_brief(bare_repo, chat_repo))
    code, _ = await run(tmp_path, home, manager, [result({"summary": "Nothing."})])
    assert code == 0
    assert not (stale / "stale.txt").exists()
    assert (stale / "chat" / "Markdown.tsx").is_file()


def test_the_prompt_lists_the_references_only_when_they_are_cloned(
    bare_repo: Path, chat_repo: Path
) -> None:
    brief = ref_brief(bare_repo, chat_repo)
    assert prompt.compose(brief, None).count("Reference repositories") == 0
    assert "Reference repositories" in prompt.compose(brief, None, ["/work/refs/x"])


def test_a_reference_path_stays_under_the_refs_folder(tmp_path: Path) -> None:
    root = tmp_path / "refs"
    assert (
        main.reference_path(root, "git.example.org:8443/g/p")
        == root / "git.example.org_8443" / "g" / "p"
    )
    assert main.reference_path(root, "github.com/../../etc") == root / "github.com" / "etc"
