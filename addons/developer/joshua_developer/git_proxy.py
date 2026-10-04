"""The git host tunnel: an HTTP ``CONNECT`` proxy on port 8002.

A worker with ``network: off`` reaches the manager only. It reaches the git
host of its task through this tunnel. The worker sets:

    HTTPS_PROXY=http://task:<TASK_TOKEN>@<MANAGER_HOST>:8002

git then sends ``CONNECT <host>:443`` with
``Proxy-Authorization: Basic base64("task:<TASK_TOKEN>")``. The proxy allows
one destination for each token: the host of the task's repository, on port
443, or on the port in the repository address when it has one. Port 22 is
never allowed. The tunnel carries TLS from end to end, so the proxy never
sees the git token.

Replies: a missing or wrong credential, or a task that ended, gets 407. Any
other destination, or a method that is not ``CONNECT``, gets 403. A host
that does not answer gets 502.

libcurl, which git uses, sends no credential on its first ``CONNECT``: it
waits for a 407 and then sends the credential again, on the same connection
when the proxy keeps it open. So a 407 is flushed and the connection stays
open for the next request, up to ``MAX_ATTEMPTS`` heads. The worker also sets
``http.proxyAuthMethod basic``, which makes curl send the credential at once.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
from collections.abc import Callable

from joshua_developer.log import get_logger
from joshua_developer.manager import Manager
from joshua_developer.repos import repo_host
from joshua_developer.store import ACTIVE_STATUSES

logger = get_logger("joshua_developer.git_proxy")

MAX_HEAD = 8192
CONNECT_TIMEOUT_S = 10.0
HEAD_TIMEOUT_S = 10.0
_CHUNK = 65536
MAX_ATTEMPTS = 3

# A token to the one host and port the token may reach, or None.
Resolver = Callable[[str], tuple[str, int] | None]


def task_target(manager: Manager, token: str) -> tuple[str, int] | None:
    """The git host and port that the task of ``token`` may reach, or None."""
    task_id = manager.store.find_task_by_worker_token(token)
    if task_id is None:
        return None
    task = manager.store.get_task(task_id)
    if task is None or task["status"] not in ACTIVE_STATUSES:
        return None
    host, _, port = repo_host(task["repo"]).partition(":")
    return host, int(port) if port else 443


def _reply(status: int, reason: str, extra: str = "") -> bytes:
    return f"HTTP/1.1 {status} {reason}\r\n{extra}Content-Length: 0\r\n\r\n".encode("latin-1")


_PROXY_AUTH = 'Proxy-Authenticate: Basic realm="developer"\r\n'


def _token_from(header: str) -> str:
    scheme, _, value = header.strip().partition(" ")
    if scheme.lower() != "basic":
        return ""
    try:
        decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return ""
    user, _, token = decoded.partition(":")
    return token if user == "task" else ""


class GitProxy:
    """A CONNECT proxy that lets each task token reach one host and port."""

    def __init__(self, resolver: Resolver) -> None:
        self.resolver = resolver
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
        for _ in range(MAX_ATTEMPTS):
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_S)
            if len(head) > MAX_HEAD:
                await self._send(writer, _reply(400, "Bad Request"))
                return
            lines = head.decode("latin-1").split("\r\n")
            parts = lines[0].split(" ")
            headers: dict[str, str] = {}
            for line in lines[1:]:
                name, sep, value = line.partition(":")
                if sep:
                    headers[name.strip().lower()] = value.strip()

            token = _token_from(headers.get("proxy-authorization", ""))
            allowed = self.resolver(token) if token else None
            if allowed is None:
                # curl sends the credential only after this challenge, on this
                # same connection. Flush, keep the connection, read the next head.
                logger.info({"message": "tunnel: credential challenge sent"})
                await self._send(writer, _reply(407, "Proxy Authentication Required", _PROXY_AUTH))
                continue

            if len(parts) != 3 or parts[0] != "CONNECT":
                await self._send(writer, _reply(403, "Forbidden"))
                return
            host, _, port_text = parts[1].rpartition(":")
            if not port_text.isdigit():
                await self._send(writer, _reply(403, "Forbidden"))
                return
            port = int(port_text)
            host = host.strip("[]").lower()
            if port == 22 or (host, port) != allowed:
                logger.warning({"message": "tunnel refused: destination not allowed", "port": port})
                await self._send(writer, _reply(403, "Forbidden"))
                return

            try:
                up_reader, up_writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), CONNECT_TIMEOUT_S
                )
            except (OSError, TimeoutError):
                logger.warning({"message": "tunnel: the git host did not answer", "host": host})
                await self._send(writer, _reply(502, "Bad Gateway"))
                return

            await self._send(writer, b"HTTP/1.1 200 Connection established\r\n\r\n")
            logger.info({"message": "tunnel open", "host": host, "port": port})
            try:
                await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
            finally:
                up_writer.close()
                with contextlib.suppress(Exception):
                    await up_writer.wait_closed()
            return
        logger.warning({"message": "tunnel refused: missing or wrong credential"})

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, data: bytes) -> None:
        writer.write(data)
        await writer.drain()


async def _pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await source.read(_CHUNK)
            if not data:
                break
            sink.write(data)
            await sink.drain()
    except ConnectionError:
        pass
    finally:
        with contextlib.suppress(Exception):
            if sink.can_write_eof():
                sink.write_eof()
