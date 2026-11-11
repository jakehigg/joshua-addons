"""The package proxy: a CONNECT proxy to the hosts of ``packages.hosts`` only.

The upstream is a TCP echo server on localhost, and ``localhost`` is the
allowed host, so no test calls a real registry.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

import pytest
from conftest import make_settings
from joshua_developer.package_proxy import PackageProxy

ALLOWED = "localhost"
LOGGER = "joshua_developer.package_proxy"


@contextlib.asynccontextmanager
async def echo_server():
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(1024):
            writer.write(data)
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


@contextlib.asynccontextmanager
async def proxy_for(*hosts: str):
    proxy = PackageProxy(hosts)
    server = await proxy.start("127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        await proxy.close()


async def send_head(
    port: int, head: str
) -> tuple[bytes, asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head.encode())
    await writer.drain()
    reply = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
    return reply, reader, writer


async def test_connect_to_an_allowed_host_relays_bytes_and_needs_no_credential() -> None:
    async with echo_server() as upstream, proxy_for(ALLOWED) as port:
        reply, reader, writer = await send_head(
            port, f"CONNECT {ALLOWED}:{upstream} HTTP/1.1\r\nHost: x\r\n\r\n"
        )
        assert reply.startswith(b"HTTP/1.1 200")
        writer.write(b"GET /some-package")
        await writer.drain()
        echoed = await asyncio.wait_for(reader.readexactly(len(b"GET /some-package")), 2)
        writer.close()
    assert echoed == b"GET /some-package"


async def test_a_host_that_is_not_in_the_list_gets_403_and_one_log_line(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        async with proxy_for(ALLOWED) as port:
            reply, _, writer = await send_head(
                port, "CONNECT registry.npmjs.org:443 HTTP/1.1\r\n\r\n"
            )
            writer.close()
    assert reply.startswith(b"HTTP/1.1 403")
    lines = [str(record.msg) for record in caplog.records if record.name == LOGGER]
    assert len(lines) == 1
    assert "registry.npmjs.org" in lines[0]
    assert "not allowed" in lines[0]


@pytest.mark.parametrize(
    "target", ["{allowed}:22", "{allowed}:https", "{allowed}", "127.0.0.1:443"]
)
async def test_a_target_that_is_not_an_allowed_host_and_port_gets_403(target: str) -> None:
    async with proxy_for(ALLOWED) as port:
        head = f"CONNECT {target.format(allowed=ALLOWED)} HTTP/1.1\r\n\r\n"
        reply, _, writer = await send_head(port, head)
        writer.close()
    assert reply.startswith(b"HTTP/1.1 403")


async def test_a_method_that_is_not_connect_gets_403() -> None:
    async with proxy_for(ALLOWED) as port:
        reply, _, writer = await send_head(port, f"GET http://{ALLOWED}/pkg HTTP/1.1\r\n\r\n")
        writer.close()
    assert reply.startswith(b"HTTP/1.1 403")


async def test_a_host_that_does_not_answer_gets_502() -> None:
    async with echo_server() as upstream:
        pass  # the port is free again
    async with proxy_for(ALLOWED) as port:
        reply, _, writer = await send_head(port, f"CONNECT {ALLOWED}:{upstream} HTTP/1.1\r\n\r\n")
        writer.close()
    assert reply.startswith(b"HTTP/1.1 502")


async def test_a_head_that_is_too_long_or_cut_is_dropped() -> None:
    async with proxy_for(ALLOWED) as port:
        reply, _, writer = await send_head(
            port, f"CONNECT {ALLOWED}:443 HTTP/1.1\r\nX: {'a' * 9000}\r\n\r\n"
        )
        writer.close()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT")
        writer.write_eof()
        closed = await asyncio.wait_for(reader.read(), 2)
        writer.close()
    assert reply.startswith(b"HTTP/1.1 400")
    assert closed == b""


def test_the_manager_starts_the_proxy_only_when_the_block_is_on(tmp_path: Path) -> None:
    from joshua_developer import server

    server.start(make_settings(tmp_path / "off"), stub_delay_s=None)
    assert server.package_proxy is None

    packages = {"enabled": True, "hosts": ["registry.npmjs.org", "pypi.org"]}
    server.start(make_settings(tmp_path / "on", config={"packages": packages}), stub_delay_s=None)
    assert server.package_proxy is not None
    assert server.package_proxy.hosts == frozenset({"registry.npmjs.org", "pypi.org"})
