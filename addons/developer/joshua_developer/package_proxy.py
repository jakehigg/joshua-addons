"""The package proxy: an HTTP ``CONNECT`` proxy on port 8003.

A worker with ``network: off`` reaches the manager only. With
``packages.enabled`` in ``developer.yaml``, the manager starts this proxy and
gives each worker ``HTTPS_PROXY`` and ``HTTP_PROXY`` that name it, so
``npm ci``, ``uv sync``, and the other package tools reach the hosts in
``packages.hosts``.

The proxy allows a ``CONNECT`` to a host in that list. Every other host gets
403 and one log line that names it. The check is on the name in the
``CONNECT`` line, not on an address, because a registry sits behind a CDN
whose addresses change.

The proxy carries no credential: a public registry needs none, and the
tunnel carries TLS from end to end, so the proxy reads nothing in it. The
list allows no git host, so the proxy gives a worker no new access to a
repository: a worker reaches a git host only through the tunnel on port
8002, which injects the token of the task.

Replies: a method that is not ``CONNECT``, a target that is not
``host:port``, a host that is not in the list, and port 22 get 403. A host
that does not answer gets 502.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable

from joshua_developer.git_proxy import (
    CONNECT_TIMEOUT_S,
    HEAD_TIMEOUT_S,
    MAX_HEAD,
    pipe,
    reply,
    send,
)
from joshua_developer.log import get_logger

logger = get_logger("joshua_developer.package_proxy")


class PackageProxy:
    """A CONNECT proxy that lets a worker reach the package hosts, and no other."""

    def __init__(self, hosts: Iterable[str]) -> None:
        self.hosts = frozenset(host.lower() for host in hosts)
        self.server: asyncio.Server | None = None

    async def start(self, host: str, port: int) -> asyncio.Server:
        self.server = await asyncio.start_server(self._handle, host, port)
        return self.server

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._serve(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        except TimeoutError:
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_S)
        if len(head) > MAX_HEAD:
            await send(writer, reply(400, "Bad Request"))
            return
        parts = head.decode("latin-1").split("\r\n")[0].split(" ")
        if len(parts) != 3 or parts[0] != "CONNECT":
            logger.warning({"message": "package proxy refused: the method is not CONNECT"})
            await send(writer, reply(403, "Forbidden"))
            return
        target, _, port_text = parts[1].rpartition(":")
        host = target.strip("[]").lower()
        if not port_text.isdigit() or int(port_text) == 22 or host not in self.hosts:
            logger.warning(
                {"message": "package proxy refused: the destination is not allowed", "host": host}
            )
            await send(writer, reply(403, "Forbidden"))
            return
        port = int(port_text)

        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), CONNECT_TIMEOUT_S
            )
        except (OSError, TimeoutError):
            logger.warning({"message": "package proxy: the host did not answer", "host": host})
            await send(writer, reply(502, "Bad Gateway"))
            return

        await send(writer, b"HTTP/1.1 200 Connection established\r\n\r\n")
        logger.info({"message": "package tunnel open", "host": host, "port": port})
        try:
            await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))
        finally:
            up_writer.close()
            with contextlib.suppress(Exception):
                await up_writer.wait_closed()
