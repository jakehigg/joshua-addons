"""The class of a repository: ``public`` or ``internal``, and the mix refusal."""

from __future__ import annotations

import pytest
from conftest import make_settings
from joshua_developer.config import ConfigError, parse_config, repo_class
from joshua_developer.locks import LockManager
from joshua_developer.manager import Manager
from joshua_developer.runtime import StubRuntime
from joshua_developer.store import TaskStore

PUBLIC = "github.com/example-home/app"
INTERNAL = "git.example.net/platform/runbooks"
CONFIG = {
    "platforms": {
        "github.com": {"kind": "github", "token_env": "GITHUB_TOKEN"},
        "git.example.net": {"kind": "git", "token_env": "GITHUB_TOKEN"},
    },
    "repos": [
        "github.com/example-home/*",
        {"pattern": "git.example.net/platform/*", "class": "internal"},
    ],
    "people": {"alex": {}, "mia": {}},
}


@pytest.fixture
def manager(data_dir):
    settings = make_settings(data_dir, config=CONFIG)
    store = TaskStore(settings.db_path)
    manager = Manager(settings, store, LockManager())
    manager.runtime = StubRuntime(manager, delay_s=None)
    yield manager
    store.close()


def test_a_pattern_with_no_class_is_public() -> None:
    config = parse_config({"repos": ["github.com/example-home/*"]})
    [pattern] = config.repos
    assert pattern.pattern == "github.com/example-home/*"
    assert pattern.repo_class == "public"


def test_a_pattern_carries_its_class() -> None:
    config = parse_config(
        {
            "repos": [{"pattern": "GIT.example.net/platform/*", "class": "internal"}],
            "people": {"alex": {"repos": [{"pattern": "github.com/alex/*", "class": "public"}]}},
        }
    )
    assert config.repos[0].pattern == "git.example.net/platform/*"
    assert config.repos[0].repo_class == "internal"
    assert config.people["alex"].repos[0].repo_class == "public"


def test_a_pattern_that_is_not_valid_is_refused() -> None:
    with pytest.raises(ConfigError, match="repos"):
        parse_config({"repos": [" "]})
    with pytest.raises(ConfigError, match="class"):
        parse_config({"repos": [{"pattern": "github.com/x/*", "class": "secret"}]})


def test_repo_class_takes_the_tighter_class() -> None:
    patterns = parse_config(
        {
            "repos": [
                "git.example.net/*",
                {"pattern": "git.example.net/platform/*", "class": "internal"},
            ]
        }
    ).repos
    assert repo_class(INTERNAL, patterns) == "internal"
    assert repo_class("git.example.net/open/app", patterns) == "public"
    assert repo_class(PUBLIC, patterns) is None


async def test_a_public_task_with_an_internal_reference_is_refused(manager: Manager) -> None:
    result = await manager.develop(
        "alex", PUBLIC, "Port the runbook.", branch="feature", refs=[INTERNAL]
    )
    assert result["status"] == "rejected"
    assert result["reason"] == "repo_class_mixed"
    assert PUBLIC in result["message"] and "public" in result["message"]
    assert INTERNAL in result["message"] and "internal" in result["message"]
    # The refusal comes before the lock and before the task row.
    assert manager.store.count_active_tasks() == 0
    assert manager.locks.get(PUBLIC, "branch:feature") is None


@pytest.mark.parametrize(
    ("repo", "refs"),
    [
        (PUBLIC, ["github.com/example-home/lib"]),
        (INTERNAL, ["git.example.net/platform/charts"]),
        (INTERNAL, [PUBLIC]),
    ],
)
async def test_a_task_inside_one_class_runs(manager: Manager, repo: str, refs: list[str]) -> None:
    result = await manager.develop("alex", repo, "Do the work.", refs=refs)
    assert result["status"] == "dispatched", result
    assert manager.store.get_task(result["task_id"])["refs"] == refs  # type: ignore[index]


async def test_the_class_comes_from_the_list_of_the_person(data_dir) -> None:
    config = {
        **CONFIG,
        "repos": ["github.com/example-home/*"],
        "people": {
            "alex": {"repos": [{"pattern": "git.example.net/platform/*", "class": "internal"}]},
            "mia": {},
        },
    }
    settings = make_settings(data_dir, config=config)
    store = TaskStore(settings.db_path)
    manager = Manager(settings, store, LockManager())
    manager.runtime = StubRuntime(manager, delay_s=None)
    try:
        assert manager.repo_class("alex", INTERNAL) == "internal"
        # mia has no entry for the host, so the repository is not in her list.
        assert manager.repo_class("mia", INTERNAL) is None
        refused = await manager.develop("alex", PUBLIC, "Port.", refs=[INTERNAL])
        assert refused["reason"] == "repo_class_mixed"
        outside = await manager.develop("mia", PUBLIC, "Port.", refs=[INTERNAL])
        assert outside["reason"] == "repo_not_allowed"
    finally:
        store.close()
