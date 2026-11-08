"""Structured JSON logging for the worker.

Each record is one JSON line with ``ts``, ``level``, ``service``, ``logger``,
and ``message``, plus any extra fields. A dict message flattens its keys into
the record. Never log the brief, a token, or the content of a file.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

SERVICE = "joshua-developer-worker"


class JSONFormatter(logging.Formatter):
    """Render a log record as one JSON line."""

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
            "service": SERVICE,
            "logger": record.name,
            "message": message,
        }
        for key, value in fields.items():
            data.setdefault(key, value)
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


def configure(level: int = logging.INFO) -> None:
    """Install one JSON stdout handler on the root logger."""
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Return a logger by name."""
    return logging.getLogger(name)
