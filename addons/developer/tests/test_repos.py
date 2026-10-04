"""Repository names, pull request references, and branch names."""

from __future__ import annotations

import pytest
from joshua_developer.repos import (
    RepoError,
    brief_title,
    check_branch,
    default_branch_name,
    normalize_repo,
    parse_pr,
    repo_allowed,
    repo_host,
    slugify,
)


@pytest.mark.parametrize(
    "raw",
    [
        "github.com/Example-Home/App",
        "https://github.com/example-home/app",
        "https://github.com/example-home/app.git",
        "https://github.com/example-home/app/",
        "http://GitHub.com/example-home/app",
        "git@github.com:example-home/app.git",
        "ssh://git@github.com/example-home/app.git",
        "https://user:ghp_SECRET@github.com/example-home/app.git",
    ],
)
def test_normalize_repo(raw: str) -> None:
    assert normalize_repo(raw) == "github.com/example-home/app"


def test_a_gitlab_subgroup_and_a_port() -> None:
    assert normalize_repo("https://gitlab.example.net/group/sub/app") == (
        "gitlab.example.net/group/sub/app"
    )
    assert normalize_repo("ssh://git@gitlab.example.net:2222/group/app.git") == (
        "gitlab.example.net:2222/group/app"
    )
    assert repo_host("gitlab.example.net:2222/group/app") == "gitlab.example.net:2222"
    assert (
        normalize_repo("gitlab.example.net:65535/group/app") == "gitlab.example.net:65535/group/app"
    )
    assert (
        normalize_repo("https://gitlab.example.net:1/group/app") == "gitlab.example.net:1/group/app"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "app",
        "example-home/app",
        "github.com/app",
        "github.com/example-home/../app",
        "ftp://github.com/example-home/app",
        "localhost/example-home/app",
        "github.com/example home/app",
        "file:///etc/passwd",
        "https://github.com:99999/example-home/app",
        "https://github.com:0/example-home/app",
        "https://github.com:x/example-home/app",
        "github.com:99999/example-home/app",
        "github.com:0/example-home/app",
        "github.com:65536/example-home/app",
    ],
)
def test_a_bad_repo_is_refused(raw: str) -> None:
    with pytest.raises(RepoError, match="repo must be a URL or host/owner/name"):
        normalize_repo(raw)


def test_a_bad_repo_message_never_repeats_the_input() -> None:
    with pytest.raises(RepoError) as exc:
        normalize_repo("https://user:ghp_SECRET@github.com/app")
    assert "ghp_SECRET" not in str(exc.value)


def test_repo_allowed() -> None:
    patterns = ["github.com/example-home/*", "GitLab.example.net/group/*"]
    assert repo_allowed("github.com/example-home/app", patterns)
    assert repo_allowed("gitlab.example.net/group/sub/app", patterns)
    assert not repo_allowed("github.com/example-homes/app", patterns)
    assert not repo_allowed("github.com/other/app", patterns)
    assert not repo_allowed("github.com/example-home/app", [])


@pytest.mark.parametrize(
    ("pr", "number", "url"),
    [
        (7, 7, None),
        ("7", 7, None),
        ("#12", 12, None),
        (
            "https://github.com/example-home/app/pull/34",
            34,
            "https://github.com/example-home/app/pull/34",
        ),
        (
            "https://gitlab.example.net/group/app/-/merge_requests/5",
            5,
            "https://gitlab.example.net/group/app/-/merge_requests/5",
        ),
    ],
)
def test_parse_pr(pr, number: int, url: str | None) -> None:
    assert parse_pr(pr) == (number, url)


@pytest.mark.parametrize("pr", [0, -1, "abc", "https://github.com/x/y/issues/3", True])
def test_a_bad_pr_is_refused(pr) -> None:
    with pytest.raises(RepoError):
        parse_pr(pr)


@pytest.mark.parametrize("name", ["main", "feature/x-1", "joshua/dev-1234abcd", "v1.2"])
def test_check_branch(name: str) -> None:
    assert check_branch(name) == name


@pytest.mark.parametrize(
    "name", ["", "-x", "a..b", "a//b", "a/", "a.lock", "has space", "a~1", "/a", "ghp_x y"]
)
def test_a_bad_branch_is_refused_without_repeating_it(name: str) -> None:
    with pytest.raises(RepoError) as exc:
        check_branch(name)
    assert str(exc.value) == "branch is not a valid branch name"


def test_default_branch_name_is_a_slug_of_the_first_line() -> None:
    brief = "\n  Add a /healthz route: café & naïve résumé — ✓ fast!!\nMore text."
    name = default_branch_name("1234abcd-0000", brief)
    assert name == "joshua/add-a-healthz-route-cafe-naive-resume-fa-1234ab"
    assert check_branch(name) == name
    assert name.isascii()


def test_default_branch_name_cuts_the_slug_to_40_characters() -> None:
    name = default_branch_name("abcdef12-3456", "Refactor " + "the parser module " * 10)
    slug = name.removeprefix("joshua/").removesuffix("-abcdef")
    assert len(slug) <= 40
    assert not slug.endswith("-")
    assert name.startswith("joshua/refactor-the-parser-module-the-")


def test_default_branch_name_without_ascii_text() -> None:
    assert default_branch_name("0a1b2c3d-0000", "日本語のタスク") == "joshua/task-0a1b2c"
    assert default_branch_name("0a1b2c3d-0000") == "joshua/task-0a1b2c"


def test_brief_title_and_slugify() -> None:
    assert brief_title("\n\n  Fix   the bug  \nbody") == "Fix the bug"
    assert brief_title("x" * 100) == "x" * 72
    assert brief_title("   \n ") == ""
    assert slugify("--Hello, World!--") == "hello-world"
