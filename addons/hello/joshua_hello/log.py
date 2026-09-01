"""Structured JSON logging for the hello addon.

Each record is one JSON line with ``ts``, ``level``, ``service``, ``logger``,
and ``message``, plus any extra fields. A dict message flattens its keys into
the record; a plain string becomes ``message``. Never log a token or a
message body at INFO.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

LEVEL_ENV = "LOG_LEVEL"


class JSONFormatter(logging.Formatter):
    """Render a log record as one JSON line."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        msg = record.msg
        fields: dict[str, Any] = {}
        if isinstance(msg, dict):
            fields = {key: value for key, value in msg.items() if key != "message"}
            message = msg.get("message", "")
        else:
            message = record.getMessage()

        data: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "message": message,
        }
        for key, value in fields.items():
            data.setdefault(key, value)
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


def _parse_level(level: str) -> int:
    raw = (level or "").strip()
    if not raw:
        return logging.INFO
    if raw.isdigit():
        return int(raw)
    resolved = logging.getLevelName(raw.upper())
    return resolved if isinstance(resolved, int) else logging.INFO


def configure(level: str, service: str) -> logging.Logger:
    """Install a JSON stdout handler on the root logger and return the service logger."""
    root = logging.getLogger()
    root.setLevel(_parse_level(level))
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter(service))
    root.addHandler(handler)
    return logging.getLogger(service)


def configure_from_env(service: str) -> logging.Logger:
    """Configure ``service`` logging from ``LOG_LEVEL`` (default ``INFO``)."""
    return configure(os.environ.get(LEVEL_ENV, "INFO"), service)


def get_logger(name: str) -> logging.Logger:
    """Return a logger by name."""
    return logging.getLogger(name)
