"""Tests for the JSON log format."""

from __future__ import annotations

import json
import logging

from joshua_developer_worker import log


def test_a_dict_message_becomes_one_json_line() -> None:
    formatter = log.JSONFormatter()
    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1, {"message": "step", "task_id": "t1"}, None, None
    )
    data = json.loads(formatter.format(record))
    assert data["message"] == "step"
    assert data["task_id"] == "t1"
    assert data["service"] == log.SERVICE


def test_a_string_message_and_an_exception() -> None:
    formatter = log.JSONFormatter()
    try:
        raise ValueError("bad")
    except ValueError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "plain", None, sys.exc_info())
    data = json.loads(formatter.format(record))
    assert data["message"] == "plain"
    assert "ValueError" in data["exception"]


def test_configure_installs_one_handler() -> None:
    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    try:
        log.configure()
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, log.JSONFormatter)
        assert log.get_logger("a").name == "a"
    finally:
        root.handlers[:] = saved
        root.setLevel(level)
