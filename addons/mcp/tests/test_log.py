"""Structured JSON logging: the formatter, the level parser, and the env hook."""

from __future__ import annotations

import json
import logging

import pytest
from joshua_mcp import log


def _make_record(msg: object, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(
        name="joshua_mcp.test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


def test_formatter_renders_a_string_message() -> None:
    formatter = log.JSONFormatter("joshua-mcp")
    line = formatter.format(_make_record("greeted Alex"))
    data = json.loads(line)
    assert data["message"] == "greeted Alex"
    assert data["service"] == "joshua-mcp"
    assert data["level"] == "INFO"
    assert data["logger"] == "joshua_mcp.test"
    assert "ts" in data


def test_formatter_flattens_a_dict_message() -> None:
    formatter = log.JSONFormatter("joshua-mcp")
    line = formatter.format(_make_record({"message": "called", "tool": "hello"}))
    data = json.loads(line)
    assert data["message"] == "called"
    assert data["tool"] == "hello"


def test_formatter_does_not_overwrite_a_reserved_field() -> None:
    formatter = log.JSONFormatter("joshua-mcp")
    line = formatter.format(_make_record({"message": "x", "service": "spoofed"}))
    data = json.loads(line)
    assert data["service"] == "joshua-mcp"


def test_formatter_includes_exception_text() -> None:
    formatter = log.JSONFormatter("joshua-mcp")
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _make_record("failed", level=logging.ERROR)
        record.exc_info = sys.exc_info()
        line = formatter.format(record)
    data = json.loads(line)
    assert "boom" in data["exception"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", logging.INFO),
        ("DEBUG", logging.DEBUG),
        ("warning", logging.WARNING),
        ("30", 30),
        ("not-a-level", logging.INFO),
    ],
)
def test_parse_level(raw: str, expected: int) -> None:
    assert log._parse_level(raw) == expected


def test_configure_installs_a_json_handler_on_root() -> None:
    logger = log.configure("DEBUG", "joshua-mcp-test")
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, log.JSONFormatter)
    assert root.level == logging.DEBUG
    assert logger.name == "joshua-mcp-test"


def test_configure_from_env_reads_log_level(monkeypatch) -> None:
    monkeypatch.setenv(log.LEVEL_ENV, "WARNING")
    log.configure_from_env("joshua-mcp-test")
    assert logging.getLogger().level == logging.WARNING


def test_configure_from_env_defaults_to_info(monkeypatch) -> None:
    monkeypatch.delenv(log.LEVEL_ENV, raising=False)
    log.configure_from_env("joshua-mcp-test")
    assert logging.getLogger().level == logging.INFO


def test_get_logger_returns_a_named_logger() -> None:
    assert log.get_logger("hello").name == "hello"
