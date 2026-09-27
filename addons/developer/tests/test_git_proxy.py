"""The git host tunnel: a CONNECT proxy that lets a task token reach its own git
host only. The upstream is a TCP echo server on localhost."""

from __future__ import annotations

import asyncio
import base64
import contextlib

import pytest
from conftest import make_settings
from joshua_developer.git_proxy import GitProxy, _token_from, task_target
from joshua_developer.locks import LockManager
from joshua_developer.manager import Manager
from joshua_developer.runtime import StubRuntime
from joshua_developer.store import TaskStore

TOKEN = "task-token-value"


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
async def proxy_for(allowed: tuple[str, int] | None):
    def resolver(token: str) -> tuple[str, int] | None:
        return allowed if token == TOKEN else None

    proxy = GitProxy(resolver)
    server = await proxy.start("127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        await proxy.close()


def auth(token: str = TOKEN, user: str = "task") -> str:
    raw = base64.b64encode(f"{user}:{token}".encode()).decode()
    return f"Proxy-Authorization: Basic {raw}\r\n"


async def send_head(
    port: int, head: str
) -> tuple[bytes, asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head.encode())
    await writer.drain()
    reply = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
    return reply, reader, writer


async def test_connect_to_the_allowed_host_relays_bytes() -> None:
    async with echo_server() as echo_port, proxy_for(("127.0.0.1", echo_port)) as port:
        reply, reader, writer = await send_head(
            port, f"CONNECT 127.0.0.1:{echo_port} HTTP/1.1\r\nHost: x\r\n{auth()}\r\n"
        )
        assert reply.startswith(b"HTTP/1.1 200")
        writer.write(b"git-upload-pack bytes")
        await writer.drain()
        echoed = await asyncio.wait_for(reader.readexactly(len(b"git-upload-pack bytes")), 2)
        writer.close()
    assert echoed == b"git-upload-pack bytes"


@pytest.mark.parametrize(
    "target",
    ["example.org:443", "127.0.0.1:22", "127.0.0.1:{other}", "127.0.0.1:https"],
)
async def test_connect_to_another_host_or_port_gets_403(target: str) -> None:
    async with echo_server() as echo_port, proxy_for(("127.0.0.1", echo_port)) as port:
        target = target.format(other=echo_port + 1)
        reply, _, writer = await send_head(port, f"CONNECT {target} HTTP/1.1\r\n{auth()}\r\n")
        writer.close()
    assert reply.startswith(b"HTTP/1.1 403")


async def test_port_22_is_refused_even_when_the_resolver_allows_it() -> None:
    async with proxy_for(("127.0.0.1", 22)) as port:
        reply, _, writer = await send_head(port, f"CONNECT 127.0.0.1:22 HTTP/1.1\r\n{auth()}\r\n")
        writer.close()
    assert reply.startswith(b"HTTP/1.1 403")


async def test_a_method_that_is_not_connect_gets_403() -> None:
    async with proxy_for(("127.0.0.1", 443)) as port:
        reply, _, writer = await send_head(
            port, f"GET http://127.0.0.1:443/ HTTP/1.1\r\n{auth()}\r\n"
        )
        writer.close()
    assert reply.startswith(b"HTTP/1.1 403")


@pytest.mark.parametrize(
    "header",
    [
        "",
        auth(token="wrong"),
        auth(user="root"),
        "Proxy-Authorization: Bearer task-token-value\r\n",
        "Proxy-Authorization: Basic !!!notbase64\r\n",
    ],
)
async def test_a_bad_credential_gets_407(header: str) -> None:
    async with proxy_for(("127.0.0.1", 443)) as port:
        reply, _, writer = await send_head(port, f"CONNECT 127.0.0.1:443 HTTP/1.1\r\n{header}\r\n")
        writer.close()
    assert reply.startswith(b"HTTP/1.1 407")
    assert b'Proxy-Authenticate: Basic realm="developer"' in reply


async def test_a_host_that_does_not_answer_gets_502() -> None:
    async with echo_server() as echo_port:
        pass  # the port is free again
    async with proxy_for(("127.0.0.1", echo_port)) as port:
        reply, _, writer = await send_head(
            port, f"CONNECT 127.0.0.1:{echo_port} HTTP/1.1\r\n{auth()}\r\n"
        )
        writer.close()
    assert reply.startswith(b"HTTP/1.1 502")


async def test_a_head_that_is_too_long_or_cut_is_dropped() -> None:
    async with proxy_for(("127.0.0.1", 443)) as port:
        reply, _, writer = await send_head(
            port, f"CONNECT 127.0.0.1:443 HTTP/1.1\r\nX: {'a' * 9000}\r\n\r\n"
        )
        writer.close()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT")
        writer.write_eof()
        closed = await asyncio.wait_for(reader.read(), 2)
        writer.close()
    assert reply.startswith(b"HTTP/1.1 400")
    assert closed == b""


def test_the_token_comes_from_basic_auth() -> None:
    encoded = base64.b64encode(b"task:abc").decode()
    assert _token_from(f"Basic {encoded}") == "abc"
    assert _token_from(f"basic {encoded}") == "abc"
    assert _token_from("Basic " + base64.b64encode(b"\xff\xfe").decode()) == ""


async def test_task_target_is_the_repo_host_on_443(tmp_path) -> None:
    settings = make_settings(
        tmp_path, config={"repos": ["github.com/example-home/*", "git.example.org:8443/*/*"]}
    )
    store = TaskStore(settings.db_path)
    manager = Manager(settings, store, LockManager())
    manager.runtime = StubRuntime(manager, delay_s=None)
    try:
        first = (await manager.develop("alex", "github.com/example-home/app", "b"))["task_id"]
        second = (await manager.develop("alex", "git.example.org:8443/g/p", "b"))["task_id"]
        tokens = {
            task_id: (store.get_task_full(task_id) or {})["worker_token"]
            for task_id in (first, second)
        }
        assert task_target(manager, tokens[first]) == ("github.com", 443)
        assert task_target(manager, tokens[second]) == ("git.example.org", 8443)
        assert task_target(manager, "wrong") is None
        store.update_task(first, status="success")
        assert task_target(manager, tokens[first]) is None
    finally:
        store.close()
