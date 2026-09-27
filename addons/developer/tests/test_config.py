"""developer.yaml and the environment. A bad setting stops the start and names the key."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import CONFIG
from joshua_developer.config import (
    ConfigError,
    builtin_personas,
    load_config,
    parse_config,
    parse_tokens,
    settings_from_env,
)


def write_config(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "developer.yaml"
    path.write_text(yaml.safe_dump(data) if not isinstance(data, str) else data)
    return path


def env_for(path: Path, **extra: str) -> dict[str, str]:
    return {"DEVELOPER_CONFIG": str(path), **extra}


def test_defaults_without_a_file(tmp_path: Path) -> None:
    settings = settings_from_env({"DEVELOPER_CONFIG": str(tmp_path / "missing.yaml")})
    assert settings.data_dir == Path("/data")
    assert settings.db_path == Path("/data/developer.db")
    assert settings.open is True
    assert settings.will_notify is False
    assert settings.worker_runtime == "stub"
    config = settings.config
    assert config.network == "off"
    assert config.default_persona == "opus"
    assert config.max_workers == 2
    assert set(config.personas) == {"sonnet", "opus", "fable"}
    assert config.personas["opus"].timeout_s == 2400
    assert config.people == {}


def test_the_plan_example_loads(tmp_path: Path) -> None:
    path = write_config(tmp_path, CONFIG)
    settings = settings_from_env(
        env_for(
            path,
            DEVELOPER_TOKENS="alex=t1, mia=t2",
            ADDON_TOKEN="t0",
            DEVELOPER_DATA_DIR="/srv/dev",
            CHANNELS_URL="http://channels:8000",
            JOSHUA_TOKEN_DEVELOPER="fleet",
            WORKER_IMAGE="example/worker:1",
        )
    )
    assert settings.data_dir == Path("/srv/dev")
    assert {c.person for c in settings.tokens.values()} == {"alex", "mia", None}
    assert settings.will_notify is True
    assert settings.worker_image == "example/worker:1"
    alex = settings.config.people["alex"]
    assert alex.git is not None and alex.git.name == "Alex Example"
    assert alex.platforms["github.com"].token_env == "GITHUB_TOKEN_ALEX"
    assert settings.config.platforms["gitlab.example.net"].kind == "gitlab"


def test_a_bare_yaml_off_and_on_mean_the_network_setting(tmp_path: Path) -> None:
    assert load_config(write_config(tmp_path, "network: off\n")).network == "off"
    assert load_config(write_config(tmp_path, "network: on\n")).network == "on"


def test_personas_in_the_file_merge_over_the_builtins() -> None:
    config = parse_config(
        {
            "default_persona": "quick",
            "personas": {
                "quick": {"model": "claude-haiku-5", "max_turns": 5, "timeout_s": 60},
                "opus": {"model": "claude-opus-5", "max_turns": 10, "timeout_s": 600},
            },
        }
    )
    assert set(config.personas) == {"quick", "sonnet", "opus", "fable"}
    assert config.personas["quick"].effort == "high"
    assert config.personas["opus"].max_turns == 10
    assert config.personas["opus"].timeout_s == 600
    assert config.personas["sonnet"] == builtin_personas()["sonnet"]
    assert set(builtin_personas()) == {"sonnet", "opus", "fable"}


def test_worker_limits_have_defaults_and_refuse_a_bad_value() -> None:
    config = parse_config({})
    assert config.worker.memory == "2g"
    assert config.worker.cpus == 2.0
    custom = parse_config({"worker": {"memory": "512m", "cpus": 0.5}})
    assert custom.worker.memory == "512m"
    for bad in ({"memory": "lots"}, {"cpus": 0}, {"swap": "1g"}):
        with pytest.raises(ConfigError) as exc:
            parse_config({"worker": bad})
        assert "worker" in str(exc.value)


@pytest.mark.parametrize(
    ("data", "key"),
    [
        ({"default_persona": "haiku"}, "default_persona"),
        ({"people": {"alex": {"default_persona": "haiku"}}}, "people.alex.default_persona"),
        ({"network": "maybe"}, "network"),
        ({"max_workers": 0}, "max_workers"),
        ({"platforms": {"github.com": {"kind": "svn", "token_env": "X"}}}, "platforms.github.com"),
        ({"platforms": {"github.com": {"kind": "github", "token_env": "lower"}}}, "token_env"),
        ({"platforms": {"GitHub.com": {"kind": "github", "token_env": "X"}}}, "platforms.GitHub"),
        ({"people": {"alex": {"platforms": {"git.example.org": {"token_env": "X"}}}}}, "kind"),
        ({"people": {"Alex": {}}}, "people.Alex"),
        ({"personas": {"Big One": {"model": "m", "max_turns": 1, "timeout_s": 1}}}, "Big One"),
        ({"surprise": 1}, "surprise"),
        ({"people": {"alex": {"git": {"name": "A", "email": "not-an-email"}}}}, "email"),
    ],
)
def test_a_bad_value_names_its_key(data: dict, key: str) -> None:
    with pytest.raises(ConfigError) as exc:
        parse_config(data)
    assert key in str(exc.value)
    assert str(exc.value).startswith("developer.yaml:")


@pytest.mark.parametrize(
    "token",
    [
        "sk-ant-oat01-SECRETSECRET",
        "glpat-SECRETSECRET",
        "ghp_SECRETSECRET",
        "github_pat_SECRETSECRET",
        "xoxb-SECRETSECRET",
    ],
)
def test_a_literal_token_is_refused_and_never_repeated(tmp_path: Path, token: str) -> None:
    data = {
        "platforms": {"github.com": {"kind": "github", "token_env": token}},
    }
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, data))
    message = str(exc.value)
    assert "platforms.github.com.token_env" in message
    assert "SECRETSECRET" not in message
    assert token not in message


def test_a_literal_token_in_a_list_or_a_key_is_refused() -> None:
    with pytest.raises(ConfigError) as exc:
        parse_config({"repos": ["github.com/x/*", "ghp_SECRET"]})
    assert "repos[1]" in str(exc.value)
    assert "ghp_SECRET" not in str(exc.value)
    with pytest.raises(ConfigError) as exc:
        parse_config({"people": {"ghp_SECRET": {}}})
    assert "people" in str(exc.value)
    assert "ghp_SECRET" not in str(exc.value)


def test_a_file_that_is_not_yaml_or_not_a_mapping(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not valid YAML at line"):
        load_config(write_config(tmp_path, "people: [\n"))
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_config(write_config(tmp_path, "- a\n- b\n"))
    assert load_config(write_config(tmp_path, "")).default_persona == "opus"


def test_a_person_in_developer_tokens_must_be_in_people(tmp_path: Path) -> None:
    path = write_config(tmp_path, CONFIG)
    with pytest.raises(ConfigError) as exc:
        settings_from_env(env_for(path, DEVELOPER_TOKENS="alex=t1,zoe=zoe-secret-token"))
    message = str(exc.value)
    assert "DEVELOPER_TOKENS" in message and "zoe" in message
    assert "zoe-secret-token" not in message and "t1" not in message


@pytest.mark.parametrize(
    ("env", "key"),
    [
        ({"DEVELOPER_TOKENS": "alex"}, "DEVELOPER_TOKENS"),
        ({"DEVELOPER_TOKENS": "Alex=t1"}, "DEVELOPER_TOKENS"),
        ({"DEVELOPER_TOKENS": "alex=t1,alex=t2"}, "DEVELOPER_TOKENS"),
        ({"DEVELOPER_TOKENS": "alex=same-secret,mia=same-secret"}, "DEVELOPER_TOKENS"),
        ({"DEVELOPER_TOKENS": "alex=same-secret", "ADDON_TOKEN": "same-secret"}, "ADDON_TOKEN"),
        ({"CHANNELS_URL": "http://channels:8000"}, "JOSHUA_TOKEN_DEVELOPER"),
        ({"JOSHUA_TOKEN_DEVELOPER": "same-secret"}, "CHANNELS_URL"),
        (
            {"CHANNELS_URL": "channels:8000", "JOSHUA_TOKEN_DEVELOPER": "same-secret"},
            "CHANNELS_URL",
        ),
        ({"WORKER_RUNTIME": "podman"}, "WORKER_RUNTIME"),
    ],
)
def test_a_bad_environment_names_the_variable(tmp_path: Path, env: dict, key: str) -> None:
    path = write_config(tmp_path, CONFIG)
    with pytest.raises(ConfigError) as exc:
        settings_from_env(env_for(path, **env))
    assert key in str(exc.value)
    assert "same-secret" not in str(exc.value)


def test_the_kubernetes_runtime_stops_the_start(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        settings_from_env(
            {"DEVELOPER_CONFIG": str(tmp_path / "x.yaml"), "WORKER_RUNTIME": "kubernetes"}
        )
    assert "the kubernetes runtime is not in this release" in str(exc.value)


def test_the_docker_runtime_needs_a_worker_image(tmp_path: Path) -> None:
    env = {"DEVELOPER_CONFIG": str(tmp_path / "x.yaml"), "WORKER_RUNTIME": "docker"}
    with pytest.raises(ConfigError, match="WORKER_IMAGE is required"):
        settings_from_env(env)
    settings = settings_from_env({**env, "WORKER_IMAGE": "example/worker:1"})
    assert settings.worker_runtime == "docker"


def test_the_worker_settings_come_from_the_environment(tmp_path: Path) -> None:
    base = {"DEVELOPER_CONFIG": str(tmp_path / "x.yaml")}
    defaults = settings_from_env(base)
    assert defaults.manager_host == "developer"
    assert defaults.worker_network == "developer_workers"
    assert defaults.public_network == "bridge"
    assert defaults.docker_host == ""
    assert defaults.claude_token == ""
    settings = settings_from_env(
        {
            **base,
            "CLAUDE_CODE_OAUTH_TOKEN": "claude-secret-value",
            "MANAGER_HOST": "dev-manager",
            "WORKER_NETWORK": "w",
            "PUBLIC_NETWORK": "p",
            "DOCKER_HOST": "tcp://docker-socket-proxy:2375",
        }
    )
    assert settings.claude_token == "claude-secret-value"
    assert "claude-secret-value" not in repr(settings)
    assert settings.manager_host == "dev-manager"
    assert (settings.worker_network, settings.public_network) == ("w", "p")
    assert settings.docker_host == "tcp://docker-socket-proxy:2375"
    with pytest.raises(ConfigError, match="MANAGER_HOST"):
        settings_from_env({**base, "MANAGER_HOST": "http://x/"})


def test_parse_tokens() -> None:
    assert parse_tokens("") == {}
    assert parse_tokens(" alex = t1 , mia=t2 ,") == {"alex": "t1", "mia": "t2"}
