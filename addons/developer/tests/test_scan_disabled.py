"""``scan.disabled_repos``: no scan for an internal repository, and the refusal
of a public pattern at load.

The value with the shape of a credential is built from fragments, so this
file holds no text that a scanner reads as a credential.
"""

from __future__ import annotations

import pytest
from conftest import FakeGitHost, FakeGitleaks, make_settings
from joshua_developer.config import ConfigError, parse_config
from joshua_developer.locks import LockManager
from joshua_developer.manager import Manager
from joshua_developer.runtime import Report, StubRuntime
from joshua_developer.scan import scan_disabled
from joshua_developer.store import TaskStore

PUBLIC = "github.com/example-home/app"
INTERNAL = "git.example.net/platform/runbooks"
SECRET = "gh" + "p_" + "Z" * 36
CONFIG = {
    "platforms": {
        "github.com": {"kind": "github", "token_env": "GITHUB_TOKEN"},
        "git.example.net": {"kind": "gitlab", "token_env": "GITLAB_TOKEN"},
    },
    "repos": [
        "github.com/example-home/*",
        {"pattern": "git.example.net/platform/*", "class": "internal"},
    ],
    "scan": {"disabled_repos": ["git.example.net/platform/*"]},
}


@pytest.fixture
def manager(data_dir):
    settings = make_settings(data_dir, config=CONFIG)
    store = TaskStore(settings.db_path)
    manager = Manager(settings, store, LockManager())
    manager.runtime = StubRuntime(manager, delay_s=None)
    yield manager
    store.close()


def pushed(head: str) -> Report:
    return Report.model_validate(
        {
            "status": "success",
            "summary": "Added the runbook.",
            "files_changed": ["runbook.md"],
            "pushed": True,
            "head": head,
        }
    )


async def push(manager: Manager, repo: str) -> dict:
    result = await manager.develop("alex", repo, "Add a runbook", branch="feature/x")
    assert result["status"] == "dispatched", result
    row = await manager.record_report(result["task_id"], pushed("feature/x"))
    assert row is not None
    return row


def test_the_default_is_an_empty_list() -> None:
    assert parse_config({}).scan.disabled_repos == []


def test_a_pattern_is_lowercase_and_stripped() -> None:
    config = parse_config(
        {
            "repos": [{"pattern": "git.example.net/platform/*", "class": "internal"}],
            "scan": {"disabled_repos": [" GIT.example.net/Platform/* "]},
        }
    )
    assert config.scan.disabled_repos == ["git.example.net/platform/*"]


def test_scan_disabled_matches_the_repository() -> None:
    patterns = ["git.example.net/platform/*"]
    assert scan_disabled(INTERNAL, patterns) is True
    assert scan_disabled(PUBLIC, patterns) is False
    assert scan_disabled(INTERNAL, []) is False


def test_a_public_pattern_in_the_list_stops_the_addon() -> None:
    with pytest.raises(ConfigError) as refused:
        parse_config(
            {
                "repos": ["github.com/example-home/*"],
                "scan": {"disabled_repos": ["github.com/example-home/app"]},
            }
        )
    assert "github.com/example-home/app" in str(refused.value)
    assert "github.com/example-home/*" in str(refused.value)
    assert "scan.disabled_repos[0]" in str(refused.value)


def test_a_public_pattern_of_a_person_in_the_list_stops_the_addon() -> None:
    with pytest.raises(ConfigError, match=r"github.com/alex-example/old"):
        parse_config(
            {
                "repos": [{"pattern": "git.example.net/platform/*", "class": "internal"}],
                "people": {"alex": {"repos": ["github.com/alex-example/*"]}},
                "scan": {"disabled_repos": ["github.com/alex-example/old"]},
            }
        )


def test_an_internal_pattern_in_the_list_loads() -> None:
    config = parse_config(CONFIG)
    assert config.scan.disabled_repos == ["git.example.net/platform/*"]


async def test_a_push_to_a_repository_in_the_list_runs_no_scan(
    manager: Manager, git_host: FakeGitHost, gitleaks: FakeGitleaks
) -> None:
    # A value with the shape of a credential in the diff changes nothing:
    # the manager reads no diff of this repository.
    git_host.compare_files = [{"new_path": "runbook.md", "diff": f"@@ -0,0 +1 @@\n+{SECRET}"}]
    row = await push(manager, INTERNAL)
    assert row["status"] == "success"
    assert row["scan"] == "disabled"
    assert row["findings"] is None
    assert gitleaks.calls == []
    assert [call for call in git_host.calls() if "compare" in call] == []
    # With no diff, the report of the task holds no diff summary.
    assert row["diff_summary"] is None
    assert git_host.calls() == []


async def test_a_push_to_a_repository_outside_the_list_is_scanned(
    manager: Manager, git_host: FakeGitHost, gitleaks: FakeGitleaks
) -> None:
    git_host.compare_files = [{"filename": "app.py", "patch": f"@@ -0,0 +1 @@\n+{SECRET}"}]
    row = await push(manager, PUBLIC)
    assert row["status"] == "failed"
    assert row["scan"] == "hit"
    assert gitleaks.calls != []
    assert [call for call in git_host.calls() if "compare" in call] != []
