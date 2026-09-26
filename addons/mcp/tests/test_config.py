"""The settings come from the environment, and a bad setting stops the start."""

from __future__ import annotations

from pathlib import Path

import pytest
from joshua_mcp.config import ConfigError, parse_tokens, settings_from_env


def test_defaults() -> None:
    settings = settings_from_env({})
    assert settings.data_dir == Path("/data")
    assert settings.wiki == Path("/data/wiki")
    assert settings.timezone.key == "UTC"
    assert settings.open is True
    assert settings.anonymous_readonly is False


def test_tokens_and_read_only_callers() -> None:
    settings = settings_from_env(
        {
            "ADDON_TOKEN": "t0",
            "MCP_TOKENS": "laptop=t1, laptop-ci=t2",
            "MCP_READONLY": "laptop-ci",
            "MCP_DATA_DIR": "/srv/joshua",
            "MCP_TIMEZONE": "America/New_York",
        }
    )
    assert settings.open is False
    assert {token: (c.name, c.readonly) for token, c in settings.tokens.items()} == {
        "t0": ("addon", False),
        "t1": ("laptop", False),
        "t2": ("laptop-ci", True),
    }
    assert settings.wiki == Path("/srv/joshua/wiki")
    assert settings.timezone.key == "America/New_York"


def test_an_anonymous_caller_can_be_read_only() -> None:
    assert settings_from_env({"MCP_READONLY": "anonymous"}).anonymous_readonly is True


@pytest.mark.parametrize(
    ("env", "match"),
    [
        ({"MCP_TOKENS": "laptop"}, "no token"),
        ({"MCP_TOKENS": "laptop="}, "no token"),
        ({"MCP_TOKENS": "Bad Name=t"}, "not a valid caller name"),
        ({"MCP_TOKENS": "a=t1,a=t2"}, "named twice"),
        ({"MCP_TOKENS": "a=t1,b=t1"}, "same token"),
        ({"MCP_TOKENS": "addon=t1", "ADDON_TOKEN": "t2"}, "ADDON_TOKEN"),
        ({"MCP_READONLY": "ghost"}, "unknown caller: ghost"),
        ({"MCP_TIMEZONE": "Mars/Olympus"}, "unknown timezone"),
    ],
)
def test_a_bad_setting_is_refused(env, match) -> None:
    with pytest.raises(ConfigError, match=match):
        settings_from_env(env)


def test_a_config_error_never_shows_a_token() -> None:
    with pytest.raises(ConfigError) as caught:
        settings_from_env({"MCP_TOKENS": "a=secret-value,b=secret-value"})
    assert "secret-value" not in str(caught.value)


def test_parse_tokens_skips_empty_items() -> None:
    assert parse_tokens(" , a=1 ,, ") == {"a": "1"}
