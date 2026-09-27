"""The report sender. It only logs, and the log names the task, never the brief."""

from __future__ import annotations

import logging

from conftest import make_settings
from joshua_developer.notify import send_report


async def test_send_report_logs_and_sends_nothing(tmp_path, caplog) -> None:
    settings = make_settings(tmp_path, channels_url="http://channels:8000", channels_token="tok")
    task = {"task_id": "t1", "status": "success", "brief": "SECRET-BRIEF", "report": {"x": 1}}
    with caplog.at_level(logging.INFO):
        sent = await send_report(settings, task)
    assert sent is False
    [record] = [r for r in caplog.records if r.name == "joshua_developer.notify"]
    assert record.msg["task_id"] == "t1"  # type: ignore[index]
    assert record.msg["channels_configured"] is True  # type: ignore[index]
    assert "SECRET-BRIEF" not in str(record.msg)
    assert "tok" not in str(record.msg.get("task_id"))  # type: ignore[union-attr]
