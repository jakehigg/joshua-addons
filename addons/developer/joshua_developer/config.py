"""Settings for the developer addon: the environment and ``developer.yaml``.

The environment holds the secrets and the addresses. ``developer.yaml`` holds
the personas, the git hosts, the repository lists, and the people. A token is
never a value in ``developer.yaml``: a platform names the environment variable
that holds its token with ``token_env``.

Every error message names the key that is wrong, and never a token value.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

DEFAULT_DATA_DIR = "/data"
DEFAULT_CONFIG = "/etc/joshua-addon/developer.yaml"
RUNTIMES = ("stub", "docker", "kubernetes")
# The runtimes this build can start. The others stop the addon at start.
IMPLEMENTED_RUNTIMES = frozenset({"stub"})

# Text that starts like one of these is a token, not a name.
TOKEN_PREFIXES = ("sk-ant-", "glpat-", "ghp_", "github_pat_", "xoxb-")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\Z")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}\Z")


class ConfigError(ValueError):
    """A setting is not valid. The message never contains a token."""


# --- developer.yaml --------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Persona(_Model):
    """One model and its limits."""

    model: str = Field(min_length=1)
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    max_turns: int = Field(gt=0)
    timeout_s: int = Field(gt=0)


def builtin_personas() -> dict[str, Persona]:
    """The personas an instance has when ``developer.yaml`` names none."""
    return {
        "sonnet": Persona(model="claude-sonnet-5", effort="medium", max_turns=60, timeout_s=1200),
        "opus": Persona(model="claude-opus-5", effort="high", max_turns=80, timeout_s=2400),
        "fable": Persona(model="claude-fable-5-1", effort="xhigh", max_turns=120, timeout_s=3600),
    }


def _check_env_name(value: str) -> str:
    if not _ENV_NAME_RE.match(value):
        raise ValueError("must be the name of an environment variable, such as GITHUB_TOKEN")
    return value


class Platform(_Model):
    """One git host: its kind, and the variable that holds its token."""

    kind: Literal["github", "gitlab", "git"]
    token_env: str

    @field_validator("token_env")
    @classmethod
    def _token_env(cls, value: str) -> str:
        return _check_env_name(value)


class PersonPlatform(_Model):
    """A person's own token for one git host. ``kind`` comes from the instance entry."""

    kind: Literal["github", "gitlab", "git"] | None = None
    token_env: str

    @field_validator("token_env")
    @classmethod
    def _token_env(cls, value: str) -> str:
        return _check_env_name(value)


class GitIdentity(_Model):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")


class Person(_Model):
    git: GitIdentity | None = None
    default_persona: str | None = None
    notify: str | None = None
    platforms: dict[str, PersonPlatform] = Field(default_factory=dict)
    repos: list[str] = Field(default_factory=list)


class DeveloperConfig(_Model):
    """The contents of ``developer.yaml``."""

    network: Literal["off", "on"] = "off"
    default_persona: str = "opus"
    max_workers: int = Field(default=2, gt=0)
    personas: dict[str, Persona] = Field(default_factory=builtin_personas)
    platforms: dict[str, Platform] = Field(default_factory=dict)
    repos: list[str] = Field(default_factory=list)
    people: dict[str, Person] = Field(default_factory=dict)

    @field_validator("network", mode="before")
    @classmethod
    def _yaml_bool(cls, value: Any) -> Any:
        # YAML 1.1 reads a bare off or on as a boolean.
        if value is True:
            return "on"
        if value is False:
            return "off"
        return value


def _find_literal_token(value: Any, path: str) -> str | None:
    """The key path of the first string that looks like a token, or None."""
    if isinstance(value, str):
        return path if value.strip().startswith(TOKEN_PREFIXES) else None
    if isinstance(value, Mapping):
        for key, item in value.items():
            here = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and key.strip().startswith(TOKEN_PREFIXES):
                return path or "<top>"
            found = _find_literal_token(item, here)
            if found:
                return found
    if isinstance(value, list):
        for index, item in enumerate(value):
            found = _find_literal_token(item, f"{path}[{index}]")
            if found:
                return found
    return None


def _loc(loc: tuple[Any, ...]) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or "<top>"


def parse_config(data: Any) -> DeveloperConfig:
    """Validate the parsed YAML of ``developer.yaml``."""
    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        raise ConfigError("developer.yaml: the top level must be a mapping")
    leaked = _find_literal_token(data, "")
    if leaked:
        raise ConfigError(
            f"developer.yaml: {leaked} holds a literal token; "
            "put the token in an environment variable and name it with token_env"
        )
    try:
        config = DeveloperConfig.model_validate(dict(data))
    except ValidationError as exc:
        first = exc.errors()[0]
        raise ConfigError(f"developer.yaml: {_loc(first['loc'])}: {first['msg']}") from None
    _check_references(config)
    return config


def _check_references(config: DeveloperConfig) -> None:
    for name in config.personas:
        if not _NAME_RE.match(name):
            raise ConfigError(f"developer.yaml: personas.{name} is not a valid persona name")
    if config.default_persona not in config.personas:
        raise ConfigError(
            f"developer.yaml: default_persona names the persona "
            f"{config.default_persona!r}, which is not in personas"
        )
    for host in config.platforms:
        _check_host(f"platforms.{host}", host)
    for person_id, person in config.people.items():
        if not _NAME_RE.match(person_id):
            raise ConfigError(f"developer.yaml: people.{person_id} is not a valid person id")
        if person.default_persona and person.default_persona not in config.personas:
            raise ConfigError(
                f"developer.yaml: people.{person_id}.default_persona names the persona "
                f"{person.default_persona!r}, which is not in personas"
            )
        for host, entry in person.platforms.items():
            key = f"people.{person_id}.platforms.{host}"
            _check_host(key, host)
            if entry.kind is None and host not in config.platforms:
                raise ConfigError(
                    f"developer.yaml: {key} needs a kind, because platforms has no entry for it"
                )


def _check_host(key: str, host: str) -> None:
    if host != host.lower() or "/" in host or not host.strip():
        raise ConfigError(f"developer.yaml: {key}: a host is a lowercase name, such as github.com")


def load_config(path: Path) -> DeveloperConfig:
    """Read and validate ``developer.yaml``. A missing file gives the defaults."""
    if not path.is_file():
        return parse_config({})
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        raise ConfigError(f"developer.yaml: the file is not valid YAML{where}") from None
    return parse_config(data)


# --- the environment -------------------------------------------------------


@dataclass(frozen=True)
class Caller:
    """One caller. ``person`` is None for ``ADDON_TOKEN`` and for an open addon."""

    person: str | None


@dataclass(frozen=True)
class Settings:
    config: DeveloperConfig
    data_dir: Path = Path(DEFAULT_DATA_DIR)
    # Bearer token to caller. Empty means no bearer is required.
    tokens: Mapping[str, Caller] = field(default_factory=dict, repr=False)
    channels_url: str | None = None
    channels_token: str = field(default="", repr=False)
    worker_image: str = ""
    worker_runtime: str = "stub"

    @property
    def open(self) -> bool:
        """True when no bearer is configured: the network is the boundary."""
        return not self.tokens

    @property
    def db_path(self) -> Path:
        return self.data_dir / "developer.db"

    @property
    def will_notify(self) -> bool:
        """True when a finished task sends its report to channels."""
        return bool(self.channels_url and self.channels_token)


def _split(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def parse_tokens(raw: str) -> dict[str, str]:
    """Parse ``DEVELOPER_TOKENS`` (``person=token,person=token``) to person to token."""
    result: dict[str, str] = {}
    for item in _split(raw):
        name, sep, token = item.partition("=")
        name = name.strip()
        token = token.strip()
        if not sep or not token:
            raise ConfigError(f"DEVELOPER_TOKENS: the entry for {name!r} has no token")
        if not _NAME_RE.match(name):
            raise ConfigError(f"DEVELOPER_TOKENS: {name!r} is not a valid person id")
        if name in result:
            raise ConfigError(f"DEVELOPER_TOKENS: the person {name!r} is named twice")
        result[name] = token
    return result


def settings_from_env(env: Mapping[str, str] | None = None) -> Settings:
    """Build the settings from ``env`` (default ``os.environ``) and ``developer.yaml``."""
    env = os.environ if env is None else env

    config = load_config(Path(env.get("DEVELOPER_CONFIG", "").strip() or DEFAULT_CONFIG))

    named = parse_tokens(env.get("DEVELOPER_TOKENS", ""))
    unknown = sorted(set(named) - set(config.people))
    if unknown:
        raise ConfigError(
            f"DEVELOPER_TOKENS names a person that is not in people in developer.yaml: "
            f"{', '.join(unknown)}"
        )
    tokens: dict[str, Caller] = {}
    for person, token in named.items():
        if token in tokens:
            raise ConfigError(f"DEVELOPER_TOKENS: {person!r} has the same token as another person")
        tokens[token] = Caller(person=person)
    addon_token = env.get("ADDON_TOKEN", "").strip()
    if addon_token:
        if addon_token in tokens:
            raise ConfigError("ADDON_TOKEN: it is the same as a token in DEVELOPER_TOKENS")
        tokens[addon_token] = Caller(person=None)

    channels_url = env.get("CHANNELS_URL", "").strip() or None
    channels_token = env.get("JOSHUA_TOKEN_DEVELOPER", "").strip()
    if channels_url and not channels_url.startswith(("http://", "https://")):
        raise ConfigError("CHANNELS_URL must start with http:// or https://")
    if bool(channels_url) != bool(channels_token):
        raise ConfigError("CHANNELS_URL and JOSHUA_TOKEN_DEVELOPER must be set together")

    runtime = env.get("WORKER_RUNTIME", "").strip() or "stub"
    if runtime not in RUNTIMES:
        raise ConfigError(f"WORKER_RUNTIME must be one of {', '.join(RUNTIMES)}")
    if runtime not in IMPLEMENTED_RUNTIMES:
        raise ConfigError(f"WORKER_RUNTIME: the {runtime} runtime is not in this release; use stub")

    return Settings(
        config=config,
        data_dir=Path(env.get("DEVELOPER_DATA_DIR", "").strip() or DEFAULT_DATA_DIR),
        tokens=tokens,
        channels_url=channels_url,
        channels_token=channels_token,
        worker_image=env.get("WORKER_IMAGE", "").strip(),
        worker_runtime=runtime,
    )
