"""The MCP client of the compose end-to-end test. Not for other use.

It calls ``develop`` on the manager with a person's bearer token, then calls
``task_status`` until the task ends or the timeout passes. It prints the
last ``task_status`` result as one line of JSON. The exit code is 0 when the
task ended, and 1 when ``develop`` was rejected or the timeout passed.

    uv run --package joshua-developer python e2e_client.py \
        <mcp url> <token> <repo> <brief> <timeout_s>
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ACTIVE = ("dispatched", "running")
POLL_S = 3.0


def payload(result: Any) -> dict[str, Any]:
    """The JSON object of a tool result."""
    if result.is_error:
        raise SystemExit(f"FAIL: the tool call failed: {result.content[0].text}")
    return json.loads(result.content[0].text)


async def run(url: str, token: str, repo: str, brief: str, timeout_s: float) -> int:
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx2.AsyncClient(headers=headers, timeout=30) as client:
        async with streamable_http_client(url, http_client=client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                args = {"repo": repo, "brief": brief}
                started = payload(await session.call_tool("develop", args))
                task_id = started.get("task_id")
                if not task_id:
                    print(json.dumps(started), flush=True)
                    return 1
                print(f"dispatched task {task_id}", file=sys.stderr, flush=True)
                deadline = time.monotonic() + timeout_s
                while True:
                    status = payload(await session.call_tool("task_status", {"task_id": task_id}))
                    if status.get("status") not in ACTIVE:
                        print(json.dumps(status), flush=True)
                        return 0
                    if time.monotonic() > deadline:
                        print(json.dumps(status), flush=True)
                        return 1
                    await asyncio.sleep(POLL_S)


def main() -> int:
    url, token, repo, brief, timeout_s = sys.argv[1:6]
    return asyncio.run(run(url, token, repo, brief, float(timeout_s)))


if __name__ == "__main__":
    sys.exit(main())
