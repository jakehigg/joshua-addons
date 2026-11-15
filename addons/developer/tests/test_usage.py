"""The usage of a task: the counts of the forwarder, the sums of a task, and
the row that goes to core. Every test is offline: core and the Claude API are
an httpx MockTransport."""

from __future__ import annotations

import json
import types
import zlib

import httpx
import pytest
from conftest import make_settings
from joshua_developer import usage
from joshua_developer.locks import LockManager
from joshua_developer.manager import Manager
from joshua_developer.runtime import StubRuntime
from joshua_developer.store import TaskStore
from joshua_developer.usage import (
    BodyCounter,
    DecodedCounter,
    StreamCounter,
    Totals,
    UsageTracker,
    counter_for,
    decoder_for,
    post_usage,
    report_task,
    usage_row,
)
from joshua_developer.worker_api import build_worker_app

FLEET = "fleet-token-value"
CLAUDE = "claude-real-token-value"
REPO = "github.com/example-home/app"
MODEL = "claude-opus-4-20250514"

# One real answer of ``v1/messages`` with ``stream: true``. The lines and the
# fields are the ones the API sends.
EVENTS = (
    b"event: message_start\n"
    b'data: {"type":"message_start","message":{"id":"msg_01","type":"message",'
    b'"role":"assistant","model":"' + MODEL.encode() + b'","content":[],'
    b'"stop_reason":null,"usage":{"input_tokens":25,"cache_creation_input_tokens":1024,'
    b'"cache_read_input_tokens":2048,"output_tokens":1}}}\n'
    b"\n"
    b"event: content_block_start\n"
    b'data: {"type":"content_block_start","index":0,'
    b'"content_block":{"type":"text","text":""}}\n'
    b"\n"
    b'event: ping\ndata: {"type": "ping"}\n'
    b"\n"
    b"event: content_block_delta\n"
    b'data: {"type":"content_block_delta","index":0,'
    b'"delta":{"type":"text_delta","text":"Hello"}}\n'
    b"\n"
    b"event: message_delta\n"
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    b'"usage":{"output_tokens":42}}\n'
    b"\n"
    b"event: message_stop\n"
    b'data: {"type":"message_stop"}\n'
    b"\n"
)

BODY = json.dumps(
    {
        "id": "msg_02",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": [{"type": "text", "text": "Hello"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 7,
            "cache_creation_input_tokens": 3,
            "cache_read_input_tokens": 11,
            "output_tokens": 5,
        },
    }
).encode()


# --- the counters ------------------------------------------------------------


def feed(counter, data: bytes, size: int = 7):
    """Give ``data`` to ``counter`` in ``size`` byte chunks, as a stream arrives."""
    for start in range(0, len(data), size):
        counter.feed(data[start : start + size])
    return counter.result()


def test_an_event_stream_gives_the_model_and_the_four_counts() -> None:
    sample = feed(StreamCounter(), EVENTS)
    assert sample is not None
    assert sample.model == MODEL
    assert sample.input == 25
    assert sample.cache_write == 1024
    assert sample.cache_read == 2048
    # message_delta holds the output of the message, not message_start.
    assert sample.output == 42


def test_an_event_stream_in_one_chunk_gives_the_same_counts() -> None:
    whole = feed(StreamCounter(), EVENTS, size=len(EVENTS))
    assert whole == feed(StreamCounter(), EVENTS, size=1)


def test_a_non_streaming_body_gives_the_model_and_the_four_counts() -> None:
    sample = feed(BodyCounter(), BODY)
    assert sample is not None
    assert (sample.model, sample.input, sample.output) == (MODEL, 7, 5)
    assert (sample.cache_read, sample.cache_write) == (11, 3)


def test_an_answer_without_counts_gives_nothing() -> None:
    assert feed(StreamCounter(), b"event: ping\ndata: {}\n\n") is None
    assert feed(StreamCounter(), b'data: {"type":"message_start","message":[]}\n\n') is None
    # An event line that is not valid JSON, and one that is not an object.
    assert feed(StreamCounter(), b'data: {"type":"message_start" oops\n\n') is None
    assert feed(StreamCounter(), b'data: "message_start"\n\n') is None
    assert feed(BodyCounter(), b"not json") is None
    assert feed(BodyCounter(), b'{"model":"m"}') is None
    assert BodyCounter().result() is None


def test_a_count_that_is_not_a_whole_number_is_zero() -> None:
    sample = usage.sample_of({"input_tokens": True, "output_tokens": -5}, model=7)
    assert sample == usage.Sample(model=None, input=0, output=0)


def test_a_line_over_the_buffer_is_not_parsed(monkeypatch) -> None:
    """A long line is dropped, and the counter reads the lines after it."""
    monkeypatch.setattr(usage, "MAX_LINE", 1024)
    long_line = b'data: {"type":"message_delta","usage":{"output_tokens":' + b"9" * 4000 + b"}}\n"
    counter = StreamCounter()
    feed(counter, long_line + EVENTS)
    sample = counter.result()
    assert sample is not None and sample.output == 42


def test_a_body_over_the_cap_is_not_parsed(monkeypatch) -> None:
    monkeypatch.setattr(usage, "MAX_JSON", 10)
    assert feed(BodyCounter(), BODY) is None


def test_only_the_messages_path_and_a_200_are_counted() -> None:
    assert isinstance(
        counter_for("v1/messages", 200, "text/event-stream; charset=utf-8"), StreamCounter
    )
    assert isinstance(counter_for("v1/messages", 200, "application/json"), BodyCounter)
    assert counter_for("v1/messages/count_tokens", 200, "application/json") is None
    assert counter_for("v1/models", 200, "application/json") is None
    assert counter_for("v1/messages", 429, "application/json") is None
    assert counter_for("v1/messages", 200, "text/plain") is None


# --- the content encoding of an answer ---------------------------------------


def gzipped(data: bytes) -> bytes:
    """``data`` as the API sends it with ``content-encoding: gzip``."""
    stream = zlib.compressobj(wbits=16 + zlib.MAX_WBITS)
    return stream.compress(data) + stream.flush()


def deflated(data: bytes) -> bytes:
    """``data`` as the API sends it with ``content-encoding: deflate``."""
    stream = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return stream.compress(data) + stream.flush()


def test_a_gzipped_stream_gives_the_counts_of_the_plain_one() -> None:
    """The chunks are smaller than the compressed answer, so one event is cut."""
    plain = feed(StreamCounter(), EVENTS)
    counter = DecodedCounter(StreamCounter(), "gzip", "t1")
    assert feed(counter, gzipped(EVENTS), size=13) == plain
    assert plain is not None and plain.output == 42


def test_a_deflated_body_gives_the_counts_of_the_plain_one() -> None:
    counter = DecodedCounter(BodyCounter(), "deflate", "t1")
    assert feed(counter, deflated(BODY), size=11) == feed(BodyCounter(), BODY)
    # A deflate answer that holds the zlib header is read as well, and an
    # empty chunk before it keeps the window to try again.
    counter = DecodedCounter(BodyCounter(), "deflate", "t1")
    counter.feed(b"")
    assert feed(counter, zlib.compress(BODY), size=11) == feed(BodyCounter(), BODY)


def test_an_answer_without_an_encoding_passes_through() -> None:
    plain = feed(StreamCounter(), EVENTS)
    for header in ("", "identity"):
        assert feed(DecodedCounter(StreamCounter(), header, "t1"), EVENTS) == plain
    # One encoding to undo beside identity: the one that applies.
    counter = DecodedCounter(StreamCounter(), "gzip, identity", "t1")
    assert feed(counter, gzipped(EVENTS)) == plain


def test_an_encoding_that_is_not_decoded_counts_nothing(caplog) -> None:
    with caplog.at_level("WARNING", logger="joshua_developer.usage"):
        counter = DecodedCounter(StreamCounter(), "snappy", "t1")
        assert feed(counter, EVENTS) is None
    assert "snappy" in caplog.text
    assert len(caplog.records) == 1
    # Two encodings to undo are not chained, and an empty name is not one.
    assert decoder_for("gzip, br") is None
    assert decoder_for("snappy") is None


def fake_library() -> types.SimpleNamespace:
    """A stand-in for brotli or zstandard: its stream gives the bytes back."""

    class Stream:
        def decompress(self, chunk: bytes) -> bytes:
            return chunk

    return types.SimpleNamespace(
        Decompressor=Stream,
        ZstdDecompressor=lambda: types.SimpleNamespace(decompressobj=Stream),
    )


def test_brotli_and_zstd_are_counted_when_a_library_is_installed(monkeypatch) -> None:
    monkeypatch.setattr(usage, "_module", lambda name: fake_library())
    plain = feed(StreamCounter(), EVENTS)
    for header in ("br", "zstd"):
        assert feed(DecodedCounter(StreamCounter(), header, "t1"), EVENTS) == plain


def test_brotli_and_zstd_count_nothing_without_a_library(monkeypatch, caplog) -> None:
    """This addon installs no brotli and no zstandard library."""
    monkeypatch.setattr(usage, "_module", lambda name: None)
    with caplog.at_level("WARNING", logger="joshua_developer.usage"):
        assert feed(DecodedCounter(StreamCounter(), "br", "t1"), EVENTS) is None
    assert "br" in caplog.text and len(caplog.records) == 1
    assert decoder_for("zstd") is None


def test_a_corrupt_stream_counts_nothing_and_raises_nothing(caplog) -> None:
    corrupt = b"\x1f\x8b" + b"not gzip at all" * 4
    with caplog.at_level("WARNING", logger="joshua_developer.usage"):
        counter = DecodedCounter(StreamCounter(), "gzip", "t1")
        assert feed(counter, corrupt) is None
    # One line for the answer, with the error type and no part of the body.
    assert len(caplog.records) == 1
    assert "not decoded" in caplog.text and "error" in caplog.text


# --- the sums of a task ------------------------------------------------------


def test_the_calls_of_a_task_add_up_and_two_tasks_stay_apart() -> None:
    tracker = UsageTracker()
    for _ in range(3):
        tracker.add("t1", feed(BodyCounter(), BODY))
    tracker.add("t2", feed(StreamCounter(), EVENTS))
    first = tracker.take("t1")
    second = tracker.take("t2")
    assert first == Totals(calls=3, input=21, output=15, cache_read=33, cache_write=9, model=MODEL)
    assert second == Totals(
        calls=1, input=25, output=42, cache_read=2048, cache_write=1024, model=MODEL
    )
    # The counts are taken out: a task is counted one time.
    assert tracker.take("t1") is None


def test_the_row_holds_the_kind_the_counts_and_the_task_id() -> None:
    row = usage_row("task-1", Totals(calls=2, input=1, output=2, cache_read=3, cache_write=4))
    assert row == {
        "kind": "developer",
        "name": "developer",
        "worker": "task-1",
        "model": None,
        "endpoint": "anthropic",
        "calls": 2,
        "usage": {"input": 1, "output": 2, "cache_read": 3, "cache_write": 4},
        "ref": "task-1",
    }


def test_a_row_keeps_to_the_contract_of_core() -> None:
    long_id = "a" * 201
    assert usage_row(long_id, Totals(calls=1)) is None
    assert usage_row("a b", Totals(calls=1)) is None
    row = usage_row("a" * 80, Totals(calls=1, input=usage.MAX_COUNT + 5, model="a model"))
    assert row is not None
    # Over 64 characters a worker name is dropped, and the ref holds the id.
    assert row["worker"] is None and len(row["ref"]) == 80
    assert row["usage"]["input"] == usage.MAX_COUNT
    # A model name that core would refuse is dropped, not sent.
    assert row["model"] is None


# --- the post to core --------------------------------------------------------


class Core:
    """A fake core: each call takes the next reply from ``replies``."""

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def core(monkeypatch) -> Core:
    fake = Core(httpx.Response(200, json={"accepted": 1}))

    def make_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))

    monkeypatch.setattr(usage, "_make_client", make_client)
    monkeypatch.setattr(usage, "RETRY_BACKOFF_S", 0)
    return fake


@pytest.fixture
def settings(tmp_path):
    return make_settings(
        tmp_path,
        core_url="http://joshua-core:8000/",
        channels_url="http://channels:8000",
        channels_token=FLEET,
        claude_token=CLAUDE,
    )


async def test_the_post_holds_the_bearer_and_one_row(settings, core) -> None:
    totals = Totals(calls=2, input=25, output=42, cache_read=2048, cache_write=1024, model=MODEL)
    assert await post_usage(settings, "t1", totals) is True
    [request] = core.requests
    assert str(request.url) == "http://joshua-core:8000/v1/usage"
    assert request.headers["authorization"] == f"Bearer {FLEET}"
    assert json.loads(request.content) == {
        "rows": [
            {
                "kind": "developer",
                "name": "developer",
                "worker": "t1",
                "model": MODEL,
                "endpoint": "anthropic",
                "calls": 2,
                "usage": {"input": 25, "output": 42, "cache_read": 2048, "cache_write": 1024},
                "ref": "t1",
            }
        ]
    }


async def test_a_400_is_not_tried_again(settings, core, caplog) -> None:
    core.replies = [httpx.Response(400, json={"detail": "unknown kind"})]
    with caplog.at_level("WARNING", logger="joshua_developer.usage"):
        assert await post_usage(settings, "t1", Totals(calls=1)) is False
    assert len(core.requests) == 1
    assert "400" in caplog.text and FLEET not in caplog.text


async def test_a_5xx_and_a_connection_error_are_tried_again(settings, core) -> None:
    core.replies = [httpx.Response(503), httpx.Response(200, json={"accepted": 1})]
    assert await post_usage(settings, "t1", Totals(calls=1)) is True
    assert len(core.requests) == 2
    core.requests.clear()
    core.replies = [httpx.ConnectError("no route")]
    assert await post_usage(settings, "t1", Totals(calls=1)) is False
    assert len(core.requests) == usage.ATTEMPTS


async def test_without_a_core_url_nothing_is_posted(tmp_path, core, caplog) -> None:
    settings = make_settings(tmp_path, channels_url="http://channels:8000", channels_token=FLEET)
    with caplog.at_level("INFO", logger="joshua_developer.usage"):
        assert await post_usage(settings, "t1", Totals(calls=1)) is False
    assert core.requests == []
    assert "core is not configured" in caplog.text


async def test_a_task_without_a_model_call_posts_nothing(settings, core) -> None:
    assert await report_task(settings, UsageTracker(), "t1") is False
    assert core.requests == []


async def test_a_task_id_that_core_would_refuse_is_not_posted(settings, core) -> None:
    assert await post_usage(settings, "a task", Totals(calls=1)) is False
    assert core.requests == []


async def test_a_task_that_reports_two_times_posts_the_same_ref(settings, core) -> None:
    tracker = UsageTracker()
    tracker.add("t1", feed(BodyCounter(), BODY))
    assert await report_task(settings, tracker, "t1") is True
    # The counts are dropped with the post, so a repeat posts only new calls.
    assert await report_task(settings, tracker, "t1") is False
    tracker.add("t1", feed(BodyCounter(), BODY))
    assert await report_task(settings, tracker, "t1") is True
    refs = [json.loads(request.content)["rows"][0]["ref"] for request in core.requests]
    assert refs == ["t1", "t1"]


# --- the forwarder and the report of a task ----------------------------------


@pytest.fixture
def manager(settings):
    store = TaskStore(settings.db_path)
    manager = Manager(settings, store, LockManager())
    manager.runtime = StubRuntime(manager, delay_s=None)
    yield manager
    store.close()


def claude_client(*answers: httpx.Response) -> httpx.AsyncClient:
    replies = list(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        return replies.pop(0) if len(replies) > 1 else replies[0]

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _stream(data: bytes):
    # Two chunks, so a counted event arrives in parts.
    yield data[:40]
    yield data[40:]


async def call_claude(manager: Manager, claude: httpx.AsyncClient, token: str, **kwargs):
    app = build_worker_app(manager, claude_client=claude)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://developer:8001",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        return await client.post("/worker/claude/v1/messages", **kwargs)


async def call_claude_raw(manager: Manager, claude: httpx.AsyncClient, token: str, **kwargs):
    """The answer of the forwarder, with the bytes as the worker's client reads them."""
    app = build_worker_app(manager, claude_client=claude)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://developer:8001",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        async with client.stream("POST", "/worker/claude/v1/messages", **kwargs) as answer:
            raw = b"".join([chunk async for chunk in answer.aiter_raw()])
    return answer, raw


async def dispatch(manager: Manager) -> tuple[str, str]:
    result = await manager.develop("alex", REPO, "Add a health check.")
    full = manager.store.get_task_full(result["task_id"])
    assert full is not None
    return full["task_id"], full["worker_token"]


async def test_the_forwarder_counts_a_stream_and_passes_it_byte_for_byte(manager) -> None:
    task_id, token = await dispatch(manager)
    claude = claude_client(
        httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_stream(EVENTS))
    )
    answer = await call_claude(manager, claude, token, json={"stream": True})
    await claude.aclose()
    assert answer.content == EVENTS
    totals = manager.usage.take(task_id)
    assert totals == Totals(
        calls=1, input=25, output=42, cache_read=2048, cache_write=1024, model=MODEL
    )


async def test_the_forwarder_counts_a_non_streaming_answer(manager) -> None:
    task_id, token = await dispatch(manager)
    claude = claude_client(
        httpx.Response(200, headers={"content-type": "application/json"}, content=_stream(BODY))
    )
    answer = await call_claude(manager, claude, token, json={"model": MODEL})
    await claude.aclose()
    assert answer.content == BODY
    totals = manager.usage.take(task_id)
    assert totals == Totals(calls=1, input=7, output=5, cache_read=11, cache_write=3, model=MODEL)


async def test_the_forwarder_counts_a_gzipped_stream_and_passes_the_raw_bytes(manager) -> None:
    """The Claude CLI asks for gzip, so the API answers with a compressed stream."""
    task_id, token = await dispatch(manager)
    raw = gzipped(EVENTS)
    claude = claude_client(
        httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "content-encoding": "gzip"},
            content=_stream(raw),
        )
    )
    answer, body = await call_claude_raw(manager, claude, token, json={"stream": True})
    await claude.aclose()
    # The worker gets the bytes of the API, gzip and all, under its headers.
    assert body == raw
    assert answer.headers["content-encoding"] == "gzip"
    totals = manager.usage.take(task_id)
    assert totals == Totals(
        calls=1, input=25, output=42, cache_read=2048, cache_write=1024, model=MODEL
    )


async def test_the_forwarder_counts_a_gzipped_non_streaming_answer(manager) -> None:
    task_id, token = await dispatch(manager)
    raw = gzipped(BODY)
    claude = claude_client(
        httpx.Response(
            200,
            headers={"content-type": "application/json", "content-encoding": "gzip"},
            content=_stream(raw),
        )
    )
    answer, body = await call_claude_raw(manager, claude, token, json={"model": MODEL})
    await claude.aclose()
    assert body == raw
    totals = manager.usage.take(task_id)
    assert totals == Totals(calls=1, input=7, output=5, cache_read=11, cache_write=3, model=MODEL)


async def test_the_forwarder_passes_an_encoding_it_does_not_decode(manager, caplog) -> None:
    task_id, token = await dispatch(manager)
    claude = claude_client(
        httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "content-encoding": "snappy"},
            content=_stream(EVENTS),
        )
    )
    with caplog.at_level("WARNING", logger="joshua_developer.usage"):
        answer, body = await call_claude_raw(manager, claude, token, json={"stream": True})
    await claude.aclose()
    assert answer.status_code == 200 and body == EVENTS
    assert manager.usage.take(task_id) is None
    assert "snappy" in caplog.text


async def test_the_forwarder_passes_a_corrupt_gzipped_answer(manager, caplog) -> None:
    task_id, token = await dispatch(manager)
    raw = b"\x1f\x8b" + b"not gzip at all" * 4
    claude = claude_client(
        httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "content-encoding": "gzip"},
            content=_stream(raw),
        )
    )
    with caplog.at_level("WARNING", logger="joshua_developer.usage"):
        answer, body = await call_claude_raw(manager, claude, token, json={"stream": True})
    await claude.aclose()
    assert answer.status_code == 200 and body == raw
    assert manager.usage.take(task_id) is None
    assert "not decoded" in caplog.text


async def test_a_counting_error_does_not_break_the_call(manager, monkeypatch) -> None:
    class Broken(usage.Counter):
        def feed(self, chunk: bytes) -> None:
            raise RuntimeError("a counting bug")

        def result(self):
            raise RuntimeError("a counting bug")

    monkeypatch.setattr(usage, "counter_for", lambda *args: Broken())
    task_id, token = await dispatch(manager)
    claude = claude_client(
        httpx.Response(200, headers={"content-type": "application/json"}, content=_stream(BODY))
    )
    answer = await call_claude(manager, claude, token, json={"model": MODEL})
    await claude.aclose()
    assert answer.status_code == 200 and answer.content == BODY
    assert manager.usage.take(task_id) is None


async def test_a_task_that_ends_reports_its_tokens_to_core(manager, core, monkeypatch) -> None:
    async def no_event(settings, task, default=None) -> bool:
        return True

    monkeypatch.setattr("joshua_developer.notify.send_report", no_event)
    task_id, token = await dispatch(manager)
    claude = claude_client(
        httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_stream(EVENTS))
    )
    await call_claude(manager, claude, token, json={"stream": True})
    await claude.aclose()
    app = build_worker_app(manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://developer:8001",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        done = await client.post("/worker/report", json={"status": "failed", "error": "stopped"})
    assert done.json() == {"recorded": True}
    [request] = core.requests
    [row] = json.loads(request.content)["rows"]
    assert row["kind"] == "developer" and row["ref"] == task_id
    assert row["calls"] == 1 and row["model"] == MODEL
    assert row["usage"] == {"input": 25, "output": 42, "cache_read": 2048, "cache_write": 1024}
    # The counts of the task are gone with the post.
    assert manager.usage.take(task_id) is None
