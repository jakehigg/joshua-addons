"""The client of the worker API on the manager.

Every call sends ``Authorization: Bearer <TASK_TOKEN>`` and ``X-Task-Id``.
A connection error gets 3 more attempts, with a longer wait each time. A 409
means the task ended; it is never retried and raises ``TaskEnded``. Any other
reply that is not 2xx raises ``ManagerError`` with the status and the reason
the manager gave.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from joshua_developer_worker.log import get_logger

logger = get_logger("joshua_developer_worker.manager_client")

RETRIES = 3
BACKOFF_S = 1.0
# The manager holds GET /worker/answer open for up to 25 s.
ANSWER_READ_S = 40.0
TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)


class Report(BaseModel):
    """The report of a task. The same fields as ``runtime.Report`` on the manager."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "failed", "blocked", "timed_out"]
    summary: str = ""
    files_changed: list[str] = Field(default_factory=list)
    tests_run: list[str] = Field(default_factory=list)
    open_question: str | None = None
    branch: str | None = None
    pushed: bool = False
    head: str | None = None
    commit_hash: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    error: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost: float | None = None
    log: str = ""


class TaskEnded(Exception):
    """The manager answered 409: the task has ended."""


class NotSent(Exception):
    """The manager stored the question, but nobody gets it (``no_destination``, ``send_failed``)."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"the question was not sent: {reason}")
        self.reason = reason


class ManagerError(Exception):
    """The manager answered with a status that is not 2xx and not 409."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(f"the manager answered {status}: {reason}")
        self.status = status
        self.reason = reason


def _reason(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return "unknown"
    if isinstance(data, dict) and isinstance(data.get("error"), str):
        return data["error"]
    return "unknown"


class ManagerClient:
    """Calls the worker API of one task."""

    def __init__(
        self,
        base_url: str,
        task_id: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        retries: int = RETRIES,
        backoff_s: float = BACKOFF_S,
    ) -> None:
        self.task_id = task_id
        self.retries = retries
        self.backoff_s = backoff_s
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "X-Task-Id": task_id},
            timeout=TIMEOUT,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        http_timeout: httpx.Timeout | None = None,
    ) -> httpx.Response:
        attempt = 0
        while True:
            try:
                response = await self._http.request(
                    method, path, json=json, timeout=http_timeout or TIMEOUT
                )
            except httpx.TransportError as exc:
                if attempt >= self.retries:
                    raise
                attempt += 1
                logger.warning(
                    {
                        "message": "the manager did not answer; trying again",
                        "task_id": self.task_id,
                        "path": path,
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                    }
                )
                await asyncio.sleep(self.backoff_s * 2 ** (attempt - 1))
                continue
            if response.status_code == 409:
                raise TaskEnded(path)
            if response.status_code >= 300:
                raise ManagerError(response.status_code, _reason(response))
            return response

    async def brief(self) -> dict[str, Any]:
        """The brief of the task, with the git token."""
        response = await self._call("GET", "/worker/brief")
        data = response.json()
        if not isinstance(data, dict):
            raise ManagerError(response.status_code, "bad_brief")
        return data

    async def running(self) -> None:
        """Tell the manager the session starts."""
        await self._call("POST", "/worker/running")

    async def log(self, text: str) -> bool:
        """Add text to the session log. Returns False when the manager dropped it."""
        response = await self._call("POST", "/worker/log", json={"text": text})
        return bool(response.json().get("appended", False))

    async def ask(self, question: str, wait_s: float, poll_pause_s: float = 0.0) -> str | None:
        """Ask the person's assistant a question and wait for the answer.

        Returns the answer, or None when ``wait_s`` seconds pass first. Raises
        TaskEnded when the task ends while the worker waits, and NotSent at
        once when the manager says nobody gets the question.
        """
        asked = await self._call("POST", "/worker/ask", json={"question": question})
        data = asked.json() if asked.content else {}
        if isinstance(data, dict) and data.get("sent") is False:
            raise NotSent(str(data.get("reason") or "unknown"))
        read = httpx.Timeout(connect=10.0, read=ANSWER_READ_S, write=30.0, pool=10.0)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            response = await self._call("GET", "/worker/answer", http_timeout=read)
            if response.status_code == 200:
                answer = response.json().get("answer")
                if isinstance(answer, str):
                    return answer
            if poll_pause_s:
                await asyncio.sleep(poll_pause_s)
        return None

    async def stop_ask(self) -> int:
        """Tell the manager the worker stopped waiting. Returns the task's paused seconds."""
        response = await self._call("POST", "/worker/ask/stop")
        return int(response.json().get("paused_s") or 0)

    async def report(self, report: Report) -> None:
        """Send the report. The task ends."""
        await self._call("POST", "/worker/report", json=report.model_dump())
