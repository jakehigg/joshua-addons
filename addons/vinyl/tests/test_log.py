"""Structured JSON logging: the formatter, the level parser, and the env hook."""

from __future__ import annotations

import json
import logging
import sys

import pytest
from joshua_vinyl import log


def _make_record(msg: object, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(
        name="vinyl.test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


def test_formatter_renders_a_string_message() -> None:
    data = json.loads(log.JSONFormatter("joshua-vinyl").format(_make_record("synced")))
    assert data["message"] == "synced"
    assert data["service"] == "joshua-vinyl"
    assert data["level"] == "INFO"
    assert data["logger"] == "vinyl.test"
    assert "ts" in data


def test_formatter_flattens_a_dict_message_and_keeps_reserved_fields() -> None:
    record = _make_record({"message": "sync finished", "count": 4, "service": "spoofed"})
    data = json.loads(log.JSONFormatter("joshua-vinyl").format(record))
    assert data["message"] == "sync finished"
    assert data["count"] == 4
    assert data["service"] == "joshua-vinyl"


def test_formatter_includes_exception_text() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = _make_record("failed", level=logging.ERROR)
        record.exc_info = sys.exc_info()
        data = json.loads(log.JSONFormatter("joshua-vinyl").format(record))
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


def test_configure_from_env_reads_log_level(monkeypatch) -> None:
    monkeypatch.setenv(log.LEVEL_ENV, "WARNING")
    logger = log.configure_from_env("joshua-vinyl-test")
    root = logging.getLogger()
    assert root.level == logging.WARNING
    assert isinstance(root.handlers[0].formatter, log.JSONFormatter)
    assert logger.name == "joshua-vinyl-test"
    assert log.get_logger("vinyl").name == "vinyl"
