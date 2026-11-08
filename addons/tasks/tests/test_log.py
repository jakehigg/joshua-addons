"""JSON log records."""

from __future__ import annotations

import json
import logging

from joshua_tasks.log import JSONFormatter, configure, get_logger


def test_dict_message_flattens_into_the_record() -> None:
    record = logging.LogRecord("t", logging.INFO, "", 0, {"message": "hi", "n": 2}, None, None)
    data = json.loads(JSONFormatter("joshua-tasks").format(record))
    assert data["message"] == "hi" and data["n"] == 2 and data["service"] == "joshua-tasks"


def test_configure_parses_level_names_and_falls_back_to_info() -> None:
    configure("debug", "joshua-tasks")
    assert logging.getLogger().level == logging.DEBUG
    configure("nonsense", "joshua-tasks")
    assert logging.getLogger().level == logging.INFO
    assert get_logger("tasks").name == "tasks"
