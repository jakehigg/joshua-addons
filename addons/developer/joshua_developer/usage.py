"""Count the tokens of each task's model calls, and report them to core.

Every model call of every worker goes through the Claude forwarder of the
worker API (``/worker/claude/{path}``), so the forwarder is the one place
that sees them all. ``counted_stream`` tees the answer of ``v1/messages`` as
it passes: a server-sent event stream holds the counts in ``message_start``
(the model and the input counts) and in ``message_delta`` (the output
count); a non-streaming answer holds both in its JSON body.
``v1/messages/count_tokens`` and the model routes use no tokens, so they are
not counted.

The API answers the Claude CLI with a content-encoded body, because the CLI
asks for one. ``DecodedCounter`` gives the counter a decoded copy of the
answer, and the worker keeps the raw bytes: the answer carries the
``content-encoding`` of the API, so only the raw bytes fit its headers.
``gzip`` and ``deflate`` are decoded with ``zlib``. ``br`` and ``zstd`` are
decoded when a decoder is installed, and counted no other way.

``UsageTracker`` holds the sum of each task in memory: the four counts, the
number of calls, and the last model. A manager restart loses the counts of
the tasks that run then.

When a task ends, ``report_task`` sends one row to
``POST <CORE_URL>/v1/usage`` with the ``JOSHUA_TOKEN_DEVELOPER`` bearer, and
drops the counts of the task. Core keeps one row for each writer and ``ref``,
so a row that comes two times is written one time. A connection error or a
5xx reply is tried again, 3 attempts in all. A 4xx reply is not tried again.
Without ``CORE_URL``, nothing is reported.

An answer that holds no counts is a warning log and nothing else, and no
error of the counting reaches the worker's call: a wrong count must never
break a task. Each answer logs one such line at most.

The log names the task and the status code, never a token and never a body.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import threading
import zlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from joshua_developer.config import Settings
from joshua_developer.log import get_logger

logger = get_logger("joshua_developer.usage")

USAGE_PATH = "/v1/usage"
REQUEST_TIMEOUT_S = 15.0
ATTEMPTS = 3
RETRY_BACKOFF_S = 2.0
# The kind and the name of every row this addon writes, and the endpoint of
# its calls.
KIND = "developer"
ENDPOINT = "anthropic"
# The one API path whose answer holds token counts.
COUNTED_PATH = "v1/messages"
# The largest count of one row: core holds it in an INT column.
MAX_COUNT = 2**31 - 1
# A label of a usage row: the worker, the model, the endpoint, and the ref.
LABEL = re.compile(r"^[A-Za-z0-9\[][A-Za-z0-9._:/@\[\]-]*$")
MAX_WORKER = 64
MAX_MODEL = 200
MAX_REF = 200
# The longest line of an event stream that the counter reads. A longer line
# is not parsed, so the buffer of a stream stays small.
MAX_LINE = 64 * 1024
# The largest non-streaming body that the counter reads.
MAX_JSON = 1024 * 1024
_DATA = b"data:"
# The window of each content encoding that ``zlib`` undoes: the window of the
# format, and the window to try again with when the first one does not read
# the first chunk. A ``deflate`` answer is raw more often than it is wrapped.
_ZLIB_WINDOWS = {
    "gzip": (16 + zlib.MAX_WBITS, None),
    "deflate": (-zlib.MAX_WBITS, zlib.MAX_WBITS),
}
# The longest content encoding name that a log line holds.
MAX_ENCODING = 100


@dataclass
class Sample:
    """The counts of one model call."""

    model: str | None = None
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0


@dataclass
class Totals:
    """The sum of the model calls of one task, with the last model seen."""

    calls: int = 0
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    model: str | None = None


def count_of(usage: Any, key: str) -> int:
    """The whole number at ``key`` of a ``usage`` object, or 0."""
    value = usage.get(key) if isinstance(usage, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def sample_of(usage: Any, model: Any = None) -> Sample:
    """The counts of one ``usage`` object of the API, with ``model`` when it is a name."""
    return Sample(
        model=model if isinstance(model, str) and model else None,
        input=count_of(usage, "input_tokens"),
        output=count_of(usage, "output_tokens"),
        cache_read=count_of(usage, "cache_read_input_tokens"),
        cache_write=count_of(usage, "cache_creation_input_tokens"),
    )


class Counter:
    """Reads the answer of one model call, chunk by chunk, and gives its counts."""

    def feed(self, chunk: bytes) -> None:
        """Take the next chunk of the answer."""
        raise NotImplementedError

    def result(self) -> Sample | None:
        """The counts of the answer, or None when it held none."""
        raise NotImplementedError


class StreamCounter(Counter):
    """The counts of a server-sent event stream.

    ``message_start`` holds the model and the input counts.
    ``message_delta`` holds the output count of the message so far, so the
    largest one is the output of the call.
    """

    def __init__(self) -> None:
        self._sample: Sample | None = None
        self._output = 0
        self._line = bytearray()
        self._dropped = False

    def feed(self, chunk: bytes) -> None:
        start = 0
        while (end := chunk.find(b"\n", start)) >= 0:
            self._line += chunk[start:end]
            self._take()
            start = end + 1
        rest = chunk[start:]
        if len(self._line) + len(rest) > MAX_LINE:
            # A line longer than a counted event: drop it to the next newline.
            self._line.clear()
            self._dropped = True
        else:
            self._line += rest

    def _take(self) -> None:
        """Read the line that is complete, and empty the buffer."""
        line = bytes(self._line)
        self._line.clear()
        if self._dropped:
            self._dropped = False
            return
        self._read(line)

    def _read(self, line: bytes) -> None:
        if not line.startswith(_DATA):
            return
        if b'"message_start"' not in line and b'"message_delta"' not in line:
            return
        try:
            event = json.loads(line[len(_DATA) :])
        except ValueError:
            logger.debug({"message": "usage: an event is not valid JSON"})
            return
        if not isinstance(event, dict):
            return
        if event.get("type") == "message_start":
            message = event.get("message")
            if isinstance(message, dict):
                self._sample = sample_of(message.get("usage"), message.get("model"))
        elif event.get("type") == "message_delta":
            self._output = max(self._output, count_of(event.get("usage"), "output_tokens"))

    def result(self) -> Sample | None:
        if self._sample is None:
            return None
        if self._output:
            self._sample.output = self._output
        return self._sample


class BodyCounter(Counter):
    """The counts of a non-streaming answer: one JSON body with ``model`` and ``usage``."""

    def __init__(self) -> None:
        self._parts: list[bytes] = []
        self._size = 0
        self._over = False

    def feed(self, chunk: bytes) -> None:
        if self._over:
            return
        self._size += len(chunk)
        if self._size > MAX_JSON:
            self._parts.clear()
            self._over = True
            logger.debug({"message": "usage: the answer is too large to count"})
            return
        self._parts.append(chunk)

    def result(self) -> Sample | None:
        if self._over or not self._parts:
            return None
        try:
            body = json.loads(b"".join(self._parts))
        except ValueError:
            logger.debug({"message": "usage: the answer is not valid JSON"})
            return None
        if not isinstance(body, dict) or not isinstance(body.get("usage"), dict):
            return None
        return sample_of(body["usage"], body.get("model"))


def counter_for(path: str, status_code: int, content_type: str) -> Counter | None:
    """The counter of one forwarded answer, or None when the answer holds no counts."""
    if path != COUNTED_PATH or status_code != 200:
        return None
    kind = content_type.split(";")[0].strip().lower()
    if kind == "text/event-stream":
        return StreamCounter()
    if kind == "application/json":
        return BodyCounter()
    return None


class Decoder:
    """Undoes the content encoding of one answer, chunk by chunk."""

    def decode(self, chunk: bytes) -> bytes:
        """The decoded bytes of the next chunk, which can be empty."""
        raise NotImplementedError


class PassDecoder(Decoder):
    """The decoder of an answer that has no content encoding."""

    def decode(self, chunk: bytes) -> bytes:
        return chunk


class ZlibDecoder(Decoder):
    """A ``gzip`` or ``deflate`` answer, decoded with ``zlib``.

    When the window of the format does not read the first chunk of bytes, and
    the format has another window, the decoder reads that chunk again with
    it.
    """

    def __init__(self, window: int, retry_window: int | None = None) -> None:
        self._stream = zlib.decompressobj(window)
        self._retry_window = retry_window

    def decode(self, chunk: bytes) -> bytes:
        if not chunk:
            return b""
        try:
            out = self._stream.decompress(chunk)
        except zlib.error:
            if self._retry_window is None:
                raise
            self._stream = zlib.decompressobj(self._retry_window)
            out = self._stream.decompress(chunk)
        self._retry_window = None
        return out


class StreamDecoder(Decoder):
    """An answer decoded by the stream object of an installed library."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream

    def decode(self, chunk: bytes) -> bytes:
        return bytes(self._stream.decompress(chunk))


def _module(name: str) -> Any | None:
    """The module ``name``, or None when it is not installed."""
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _brotli_decoder() -> Decoder | None:
    for name in ("brotli", "brotlicffi"):
        if (module := _module(name)) is not None:
            return StreamDecoder(module.Decompressor())
    return None


def _zstd_decoder() -> Decoder | None:
    if (module := _module("zstandard")) is None:
        return None
    return StreamDecoder(module.ZstdDecompressor().decompressobj())


def decoder_for(content_encoding: str) -> Decoder | None:
    """The decoder of one answer, or None when the counter cannot read it.

    An answer with no encoding, with an empty one, or with ``identity`` gets
    a decoder that passes the chunks on. A header with more than one encoding
    to undo, an encoding that this addon does not know, and ``br`` or
    ``zstd`` with no library installed all give None.
    """
    names = [part.strip().lower() for part in content_encoding.split(",")]
    names = [name for name in names if name and name != "identity"]
    if not names:
        return PassDecoder()
    if len(names) > 1:
        return None
    name = names[0]
    if name in _ZLIB_WINDOWS:
        return ZlibDecoder(*_ZLIB_WINDOWS[name])
    if name == "br":
        return _brotli_decoder()
    if name == "zstd":
        return _zstd_decoder()
    return None


class DecodedCounter(Counter):
    """``counter``, fed the decoded bytes of a content-encoded answer.

    An answer that has no decoder, and an answer that a decoder cannot read,
    hold no counts: the wrapper logs one warning and gives the counter
    nothing more. The log names the encoding or the error, never a count and
    never a part of the body.
    """

    def __init__(self, counter: Counter, content_encoding: str, task_id: str) -> None:
        self._counter = counter
        self._task_id = task_id
        self._decoder = decoder_for(content_encoding)
        if self._decoder is None:
            logger.warning(
                {
                    "message": "usage: the answer encoding is not decoded, so it is not counted",
                    "task_id": task_id,
                    "content_encoding": content_encoding[:MAX_ENCODING],
                }
            )

    def feed(self, chunk: bytes) -> None:
        if self._decoder is None:
            return
        try:
            decoded = self._decoder.decode(chunk)
        except Exception as exc:
            self._decoder = None
            logger.warning(
                {
                    "message": "usage: the answer was not decoded, so it is not counted",
                    "task_id": self._task_id,
                    "error_type": type(exc).__name__,
                }
            )
            return
        self._counter.feed(decoded)

    def result(self) -> Sample | None:
        if self._decoder is None:
            return None
        return self._counter.result()


class UsageTracker:
    """The token counts of each task that runs, in the memory of the manager."""

    def __init__(self) -> None:
        self._totals: dict[str, Totals] = {}
        self._lock = threading.Lock()

    def add(self, task_id: str, sample: Sample) -> None:
        """Add the counts of one model call of ``task_id``."""
        with self._lock:
            totals = self._totals.setdefault(task_id, Totals())
            totals.calls += 1
            totals.input += sample.input
            totals.output += sample.output
            totals.cache_read += sample.cache_read
            totals.cache_write += sample.cache_write
            if sample.model:
                totals.model = sample.model

    def take(self, task_id: str) -> Totals | None:
        """The totals of ``task_id``, taken out of the tracker. None when it has none."""
        with self._lock:
            return self._totals.pop(task_id, None)


def counted_stream(
    source: AsyncIterator[bytes], counter: Counter, tracker: UsageTracker, task_id: str
) -> AsyncIterator[bytes]:
    """``source``, byte for byte, with the counts of the answer on the way.

    The counting never changes a chunk, never holds one back, and never
    raises into the forwarder. The first chunk that the counter cannot read
    ends the counting of the answer, so one answer logs one warning at most.
    """

    async def stream() -> AsyncIterator[bytes]:
        counting = True
        try:
            async for chunk in source:
                if counting:
                    counting = _feed(counter, chunk, task_id)
                yield chunk
        finally:
            _commit(counter, tracker, task_id)

    return stream()


def _feed(counter: Counter, chunk: bytes, task_id: str) -> bool:
    """Give ``chunk`` to ``counter``. False when the counter raised on it."""
    try:
        counter.feed(chunk)
    except Exception as exc:
        logger.warning(
            {
                "message": "usage: a chunk was not counted",
                "task_id": task_id,
                "error_type": type(exc).__name__,
            }
        )
        return False
    return True


def _commit(counter: Counter, tracker: UsageTracker, task_id: str) -> None:
    try:
        sample = counter.result()
    except Exception as exc:
        logger.warning(
            {
                "message": "usage: the answer was not counted",
                "task_id": task_id,
                "error_type": type(exc).__name__,
            }
        )
        return
    if sample is None:
        logger.warning({"message": "usage: the answer held no counts", "task_id": task_id})
        return
    tracker.add(task_id, sample)


def label(value: str | None, limit: int) -> str | None:
    """``value`` when it is a usage label that core accepts, else None."""
    if not value or len(value) > limit or not LABEL.match(value):
        return None
    return value


def _clamp(value: int) -> int:
    return min(max(value, 0), MAX_COUNT)


def usage_row(task_id: str, totals: Totals) -> dict[str, Any] | None:
    """The usage row of one task, or None when the task id is not a valid ref."""
    ref = label(task_id, MAX_REF)
    if ref is None:
        logger.debug({"message": "usage: the task id is not a valid ref"})
        return None
    return {
        "kind": KIND,
        "name": KIND,
        "worker": label(task_id, MAX_WORKER),
        "model": label(totals.model, MAX_MODEL),
        "endpoint": ENDPOINT,
        "calls": _clamp(totals.calls),
        "usage": {
            "input": _clamp(totals.input),
            "output": _clamp(totals.output),
            "cache_read": _clamp(totals.cache_read),
            "cache_write": _clamp(totals.cache_write),
        },
        "ref": ref,
    }


def _make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S)


async def post_usage(settings: Settings, task_id: str, totals: Totals) -> bool:
    """Post the row of one task to core. Returns True when core accepted it."""
    if not settings.core_url or not settings.channels_token:
        logger.info({"message": "usage not reported: core is not configured", "task_id": task_id})
        return False
    row = usage_row(task_id, totals)
    if row is None:
        return False
    url = settings.core_url.rstrip("/") + USAGE_PATH
    headers = {"Authorization": f"Bearer {settings.channels_token}"}
    where = {"task_id": task_id}
    async with _make_client() as client:
        for attempt in range(1, ATTEMPTS + 1):
            try:
                response = await client.post(url, json={"rows": [row]}, headers=headers)
            except httpx.HTTPError as exc:
                logger.warning(
                    {
                        "message": "usage attempt failed",
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                        **where,
                    }
                )
            else:
                if response.status_code == 200:
                    logger.info({"message": "usage reported", **where})
                    return True
                logger.warning(
                    {
                        "message": "core refused the usage",
                        "status_code": response.status_code,
                        **where,
                    }
                )
                if response.status_code < 500:
                    return False
            if attempt < ATTEMPTS:
                await asyncio.sleep(RETRY_BACKOFF_S * attempt)
    logger.error({"message": "usage not sent after retries", **where})
    return False


async def report_task(settings: Settings, tracker: UsageTracker, task_id: str) -> bool:
    """Report the counts of a task that ended, and drop them.

    A task that made no model call reports nothing. The counts go out of the
    tracker before the post, so a failed post does not hold memory. Core
    keeps one row for each writer and ``ref``, so a task that reports two
    times writes one row.
    """
    totals = tracker.take(task_id)
    if totals is None or not totals.calls:
        return False
    return await post_usage(settings, task_id, totals)
