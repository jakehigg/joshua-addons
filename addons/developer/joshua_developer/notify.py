"""Send the report of a finished task to the person's chat, through channels.

The report goes as an event to ``POST <CHANNELS_URL>/v1/events`` with the
``JOSHUA_TOKEN_DEVELOPER`` bearer. This module only logs the report for now.
"""

from __future__ import annotations

from typing import Any

from joshua_developer.config import Settings
from joshua_developer.log import get_logger

logger = get_logger("joshua_developer.notify")


async def send_report(settings: Settings, task: dict[str, Any]) -> bool:
    """Send the report of ``task`` to ``task["notify"]``. Returns True when it was sent.

    ``task`` is the full task row. The log names the task, never the brief or
    the report text.
    """
    # TODO(P3.2): post the event to channels, with the retry rules of v3 notify.py.
    logger.info(
        {
            "message": "task report not sent: the channels client is not built",
            "task_id": task.get("task_id"),
            "status": task.get("status"),
            "channels_configured": settings.will_notify,
        }
    )
    return False
