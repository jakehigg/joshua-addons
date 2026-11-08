"""Structured JSON logging for the developer addon.

Each record is one JSON line with ``ts``, ``level``, ``service``, ``logger``,
and ``message``, plus any extra fields. A dict message flattens its keys into
the record; a plain string becomes ``message``. Never log a token or a
message body at INFO.

uvicorn writes one access line for each request, on the ``uvicorn.access``
logger. ``QuietHealthChecks`` moves the line of a ``GET /healthz`` on
either port to DEBUG: compose and Kubernetes call it every few seconds.
The access lines of ``/mcp`` and ``/worker/*`` stay at INFO.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

LEVEL_ENV = "LOG_LEVEL"
ACCESS_LOGGER = "uvicorn.access"
# The paths whose access line is DEBUG, not INFO.
QUIET_PATHS = frozenset({"/healthz"})


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


class QuietHealthChecks(logging.Filter):
    """Move the uvicorn access line of a ``QUIET_PATHS`` request to DEBUG.

    uvicorn logs ``'%s - "%s %s HTTP/%s" %d'`` with the client, the method,
    the path with its query, the HTTP version, and the status. The filter
    reads the path from those arguments. It drops the line unless the root
    logger shows DEBUG.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 3:
            return True
        path = str(args[2]).split("?", 1)[0]
        if path not in QUIET_PATHS or record.levelno != logging.INFO:
            return True
        record.levelno = logging.DEBUG
        record.levelname = logging.getLevelName(logging.DEBUG)
        # Not isEnabledFor: while a logger handles a record, Python 3.13
        # answers False for every logger, as a guard against recursion.
        return logging.getLogger().getEffectiveLevel() <= logging.DEBUG


def quiet_health_checks() -> None:
    """Put one ``QuietHealthChecks`` on the uvicorn access logger."""
    access = logging.getLogger(ACCESS_LOGGER)
    if not any(isinstance(item, QuietHealthChecks) for item in access.filters):
        access.addFilter(QuietHealthChecks())


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
    quiet_health_checks()
    return logging.getLogger(service)


def configure_from_env(service: str) -> logging.Logger:
    """Configure ``service`` logging from ``LOG_LEVEL`` (default ``INFO``)."""
    return configure(os.environ.get(LEVEL_ENV, "INFO"), service)


def get_logger(name: str) -> logging.Logger:
    """Return a logger by name."""
    return logging.getLogger(name)
