"""Settings for the joshua-mcp addon, read from the environment.

Every setting is documented in the addon's README.md and values.yaml:

- ``MCP_DATA_DIR``: the joshua-ai data volume. The wiki is ``<dir>/wiki``.
- ``ADDON_TOKEN``: one bearer, for the caller named ``addon``.
- ``MCP_TOKENS``: one bearer for each caller, as ``name=token`` pairs.
- ``MCP_READONLY``: the callers that can only read, as a list of names.
- ``MCP_TIMEZONE``: the timezone that sets "today" for the journal.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_DATA_DIR = "/data"
DEFAULT_TIMEZONE = "UTC"
# The caller name that ADDON_TOKEN authenticates.
ADDON_CALLER = "addon"
# The caller name when no bearer is configured and the network is the boundary.
ANONYMOUS = "anonymous"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\Z")


class ConfigError(ValueError):
    """A setting is not valid. The message never contains a token."""


@dataclass(frozen=True)
class Caller:
    """One caller: the name a write records, and whether it can write."""

    name: str
    readonly: bool = False


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    timezone: ZoneInfo
    # Bearer token to caller. Empty means no bearer is required.
    tokens: Mapping[str, Caller] = field(default_factory=dict)
    # The caller when no bearer is configured.
    anonymous_readonly: bool = False

    @property
    def wiki(self) -> Path:
        return self.data_dir / "wiki"

    @property
    def open(self) -> bool:
        """True when no bearer is configured: the network is the boundary."""
        return not self.tokens


def _split(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def parse_tokens(raw: str) -> dict[str, str]:
    """Parse ``MCP_TOKENS`` (``name=token,name=token``) to name to token."""
    result: dict[str, str] = {}
    for item in _split(raw):
        name, sep, token = item.partition("=")
        name = name.strip()
        token = token.strip()
        if not sep or not token:
            raise ConfigError(f"MCP_TOKENS: the entry for {name!r} has no token")
        if not _NAME_RE.match(name):
            raise ConfigError(f"MCP_TOKENS: {name!r} is not a valid caller name")
        if name in result:
            raise ConfigError(f"MCP_TOKENS: the caller {name!r} is named twice")
        result[name] = token
    return result


def settings_from_env(env: Mapping[str, str] | None = None) -> Settings:
    """Build the settings from ``env`` (default ``os.environ``)."""
    env = os.environ if env is None else env

    named = parse_tokens(env.get("MCP_TOKENS", ""))
    addon_token = env.get("ADDON_TOKEN", "").strip()
    if addon_token:
        if ADDON_CALLER in named:
            raise ConfigError(f"MCP_TOKENS: {ADDON_CALLER!r} is the name of ADDON_TOKEN")
        named[ADDON_CALLER] = addon_token

    readonly = set(_split(env.get("MCP_READONLY", "")))
    known = set(named) | {ANONYMOUS}
    unknown = sorted(readonly - known)
    if unknown:
        raise ConfigError(f"MCP_READONLY names an unknown caller: {', '.join(unknown)}")

    tokens: dict[str, Caller] = {}
    for name, token in named.items():
        if token in tokens:
            raise ConfigError(f"MCP_TOKENS: {name!r} has the same token as another caller")
        tokens[token] = Caller(name=name, readonly=name in readonly)

    tz_name = env.get("MCP_TIMEZONE", "").strip() or DEFAULT_TIMEZONE
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"MCP_TIMEZONE: unknown timezone {tz_name!r}") from exc

    return Settings(
        data_dir=Path(env.get("MCP_DATA_DIR", "").strip() or DEFAULT_DATA_DIR),
        timezone=tz,
        tokens=tokens,
        anonymous_readonly=ANONYMOUS in readonly,
    )
