"""Structured JSON logging: the formatter, the level parser, and the env hook."""

from __future__ import annotations

import json
import logging

import pytest
from joshua_developer import log


def _make_record(msg: object, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(
        name="joshua_developer.test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


def test_formatter_renders_a_string_message() -> None:
    formatter = log.JSONFormatter("joshua-developer")
    line = formatter.format(_make_record("greeted Alex"))
    data = json.loads(line)
    assert data["message"] == "greeted Alex"
    assert data["service"] == "joshua-developer"
    assert data["level"] == "INFO"
    assert data["logger"] == "joshua_developer.test"
    assert "ts" in data


def test_formatter_flattens_a_dict_message() -> None:
    formatter = log.JSONFormatter("joshua-developer")
    line = formatter.format(_make_record({"message": "called", "tool": "hello"}))
    data = json.loads(line)
    assert data["message"] == "called"
    assert data["tool"] == "hello"


def test_formatter_does_not_overwrite_a_reserved_field() -> None:
    formatter = log.JSONFormatter("joshua-developer")
    line = formatter.format(_make_record({"message": "x", "service": "spoofed"}))
    data = json.loads(line)
    assert data["service"] == "joshua-developer"


def test_formatter_includes_exception_text() -> None:
    formatter = log.JSONFormatter("joshua-developer")
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
    logger = log.configure("DEBUG", "joshua-developer-test")
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, log.JSONFormatter)
    assert root.level == logging.DEBUG
    assert logger.name == "joshua-developer-test"


def test_configure_from_env_reads_log_level(monkeypatch) -> None:
    monkeypatch.setenv(log.LEVEL_ENV, "WARNING")
    log.configure_from_env("joshua-developer-test")
    assert logging.getLogger().level == logging.WARNING


def test_configure_from_env_defaults_to_info(monkeypatch) -> None:
    monkeypatch.delenv(log.LEVEL_ENV, raising=False)
    log.configure_from_env("joshua-developer-test")
    assert logging.getLogger().level == logging.INFO


def test_get_logger_returns_a_named_logger() -> None:
    assert log.get_logger("hello").name == "hello"


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


async def _access_records(level: str, paths: list[str]) -> list[logging.LogRecord]:
    """Serve a small app with uvicorn on loopback, GET each path, return the access records."""
    import asyncio

    import httpx
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def ok(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route(p.split("?")[0], ok) for p in paths])
    log.configure(level, "joshua-developer-test")
    collect = _Collect()
    logging.getLogger().addHandler(collect)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None))
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    serving = asyncio.create_task(server.serve())
    try:
        while not server.started:  # noqa: ASYNC110 - uvicorn has no started event
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            for path in paths:
                assert (await client.get(path)).status_code == 200
    finally:
        server.should_exit = True
        await serving
        logging.getLogger().removeHandler(collect)
    return [r for r in collect.records if r.name == log.ACCESS_LOGGER]


async def test_a_healthz_request_makes_no_info_record() -> None:
    records = await _access_records(
        "INFO", ["/healthz", "/healthz?probe=1", "/mcp", "/worker/brief"]
    )
    paths = [r.args[2] for r in records]
    assert "/healthz" not in paths and "/healthz?probe=1" not in paths
    assert "/mcp" in paths and "/worker/brief" in paths
    assert all(r.levelno == logging.INFO for r in records)


async def test_a_healthz_request_logs_at_debug_when_debug_is_on() -> None:
    records = await _access_records("DEBUG", ["/healthz", "/mcp"])
    levels = {r.args[2]: r.levelno for r in records}
    assert levels == {"/healthz": logging.DEBUG, "/mcp": logging.INFO}


def test_quiet_health_checks_installs_one_filter() -> None:
    log.quiet_health_checks()
    log.quiet_health_checks()
    access = logging.getLogger(log.ACCESS_LOGGER)
    assert sum(isinstance(f, log.QuietHealthChecks) for f in access.filters) == 1


def test_the_filter_passes_a_record_without_access_arguments() -> None:
    record = _make_record("plain text")
    assert log.QuietHealthChecks().filter(record) is True
    assert record.levelno == logging.INFO
