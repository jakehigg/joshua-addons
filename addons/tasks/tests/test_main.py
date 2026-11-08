"""``python -m joshua_tasks`` serves on 0.0.0.0:8000."""

from __future__ import annotations

from joshua_tasks import __main__ as entry


def test_main_runs_uvicorn_on_all_interfaces(monkeypatch) -> None:
    calls = {}
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kw: calls.update(kw))
    monkeypatch.setattr(entry, "configure_from_env", lambda service: None)
    entry.main()
    assert calls["host"] == "0.0.0.0" and calls["port"] == 8000
