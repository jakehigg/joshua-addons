"""In-process change broadcaster feeding the /api/events SSE stream.

Every mutation in the service layer publishes a small event; open browser
tabs subscribe and refetch what changed. Because the REST API and the MCP
server run in the same process against the same service layer, agent-made
changes reach open tabs the same way UI-made ones do.

Single-replica by design — with more than one replica this would need
Postgres LISTEN/NOTIFY instead of an in-process set of queues.
"""

import asyncio
from contextvars import ContextVar

# The mutating client's self-assigned id (X-Client-Id header), echoed back in
# events so a tab can ignore the changes it made itself. None for MCP calls.
current_origin: ContextVar[str | None] = ContextVar("current_origin", default=None)


class Broadcaster:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, scope: str, project_id: str | None = None) -> None:
        event = {
            "scope": scope,  # "projects" (list changed) | "tasks" (one project's content)
            "project_id": project_id,
            "origin": current_origin.get(),
        }
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A wedged subscriber loses events; its reconnect refetches.
                pass


broadcaster = Broadcaster()
