"""
Tests for the SSE streaming lifecycle: bounded channels, cancellation,
client disconnect, producer errors, reconnect fields, and wire format.

The disconnect/slow-send tests drive the ASGI app directly (without
TestClient) so they can trigger a real `http.disconnect` mid-stream and
control how fast the downstream `send()` consumes events.
"""

import json
import threading
import time
from collections.abc import AsyncIterable, AsyncIterator, Iterable
from typing import Any

import anyio
import fastapi.routing
import pytest
from fastapi import BackgroundTasks, Depends, FastAPI
from fastapi.exceptions import ResponseValidationError
from fastapi.responses import EventSourceResponse
from fastapi.sse import ServerSentEvent, format_sse_event
from fastapi.testclient import TestClient
from pydantic import BaseModel


class Item(BaseModel):
    name: str


def make_scope(path: str) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }


class DisconnectHarness:
    """ASGI receive/send pair that disconnects after N body chunks.

    Optionally slows down the downstream send() to simulate a client
    that consumes events slower than the producer generates them.
    """

    def __init__(self, disconnect_after: int, send_delay: float = 0.0) -> None:
        self.disconnect_after = disconnect_after
        self.send_delay = send_delay
        self.body_chunks = 0
        self.sent_messages: list[dict[str, Any]] = []
        self._request_sent = False

    async def receive(self) -> dict[str, Any]:
        if not self._request_sent:
            self._request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        while self.body_chunks < self.disconnect_after:
            await anyio.sleep(0.001)
        return {"type": "http.disconnect"}

    async def send(self, message: dict[str, Any]) -> None:
        self.sent_messages.append(message)
        if message["type"] == "http.response.body" and message.get("more_body"):
            if self.send_delay:
                await anyio.sleep(self.send_delay)
            self.body_chunks += 1

    @property
    def body_bytes(self) -> bytes:
        return b"".join(
            message.get("body", b"")
            for message in self.sent_messages
            if message["type"] == "http.response.body"
        )


def test_disconnect_stops_infinite_generator_and_closes_it():
    """On client disconnect the producer stops promptly (bounded queue)
    and the endpoint generator is closed deterministically."""
    produced: list[int] = []
    cleaned: list[str] = []
    app = FastAPI()

    @app.get("/infinite", response_class=EventSourceResponse)
    async def infinite() -> AsyncIterable[dict[str, int]]:
        try:
            n = 0
            while True:
                produced.append(n)
                yield {"n": n}
                n += 1
        finally:
            cleaned.append("generator-closed")

    async def main() -> DisconnectHarness:
        harness = DisconnectHarness(disconnect_after=3, send_delay=0.02)
        await app(make_scope("/infinite"), harness.receive, harness.send)
        return harness

    harness = anyio.run(main)

    # The generator was closed (its finally block ran) before the
    # ASGI app returned, not left for garbage collection.
    assert cleaned == ["generator-closed"]
    # Backpressure: with a slow consumer the producer cannot run ahead
    # more than the bounded hand-off channels allow.
    # (3 sent + at most one item in each of: both channel buffers, the
    # producer hand-off, the keepalive inserter, and the generator.)
    assert len(produced) <= 3 + 6
    # Events were sent in order, one SSE event per body chunk.
    assert b'data: {"n":0}' in harness.body_bytes
    assert (
        harness.body_bytes.index(b'"n":0')
        < harness.body_bytes.index(b'"n":1')
        < harness.body_bytes.index(b'"n":2')
    )


def test_disconnect_exits_yield_dependencies():
    """Dependencies with yield (e.g. DB sessions) exit on disconnect."""
    events: list[str] = []
    app = FastAPI()

    async def get_db() -> AsyncIterator[str]:
        events.append("db-enter")
        yield "db"
        events.append("db-exit")

    @app.get("/dep", response_class=EventSourceResponse)
    async def dep_stream(db: str = Depends(get_db)) -> AsyncIterable[dict[str, int]]:
        assert db == "db"
        n = 0
        while True:
            yield {"n": n}
            n += 1
            await anyio.sleep(0.005)

    async def main() -> None:
        harness = DisconnectHarness(disconnect_after=2)
        await app(make_scope("/dep"), harness.receive, harness.send)

    anyio.run(main)
    assert events == ["db-enter", "db-exit"]


def test_disconnect_during_idle_keepalive_still_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
):
    """Disconnecting while the generator is idle (keepalive phase)
    cancels the producer and closes the generator."""
    monkeypatch.setattr(fastapi.routing, "_PING_INTERVAL", 0.02)
    cleaned: list[str] = []
    app = FastAPI()

    @app.get("/idle", response_class=EventSourceResponse)
    async def idle() -> AsyncIterable[dict[str, int]]:
        try:
            yield {"n": 1}
            # Idle for much longer than the ping interval: keepalive
            # comments are emitted while we wait here.
            await anyio.sleep(30)
            yield {"n": 2}  # pragma: no cover
        finally:
            cleaned.append("generator-closed")

    async def main() -> DisconnectHarness:
        # 1 real event + 1 keepalive comment, then disconnect.
        harness = DisconnectHarness(disconnect_after=2)
        await app(make_scope("/idle"), harness.receive, harness.send)
        return harness

    harness = anyio.run(main)
    assert cleaned == ["generator-closed"]
    assert b'{"n":1}' in harness.body_bytes
    assert b": ping" in harness.body_bytes


def test_disconnect_closes_sync_generator():
    """Sync generators (run in a worker thread) are closed on disconnect."""
    cleaned: list[str] = []
    app = FastAPI()

    @app.get("/sync-infinite", response_class=EventSourceResponse)
    def sync_infinite() -> Iterable[dict[str, int]]:
        try:
            n = 0
            while True:
                yield {"n": n}
                n += 1
                time.sleep(0.005)
        finally:
            cleaned.append("sync-generator-closed")

    async def main() -> DisconnectHarness:
        harness = DisconnectHarness(disconnect_after=2, send_delay=0.02)
        await app(make_scope("/sync-infinite"), harness.receive, harness.send)
        return harness

    anyio.run(main)
    assert cleaned == ["sync-generator-closed"]


def test_failing_send_stops_producer_and_cleans_up():
    """If the ASGI send() fails (broken connection), the error is not
    swallowed and the generator is still closed."""
    cleaned: list[str] = []
    app = FastAPI()

    @app.get("/infinite", response_class=EventSourceResponse)
    async def infinite() -> AsyncIterable[dict[str, int]]:
        try:
            n = 0
            while True:
                yield {"n": n}
                n += 1
        finally:
            cleaned.append("generator-closed")

    class FailingSendHarness(DisconnectHarness):
        async def send(self, message: dict[str, Any]) -> None:
            if self.body_chunks >= 2:
                raise OSError("connection reset")
            await super().send(message)

    async def main() -> None:
        harness = FailingSendHarness(disconnect_after=10**9)
        await app(make_scope("/infinite"), harness.receive, harness.send)

    with pytest.raises(OSError, match="connection reset"):
        anyio.run(main)
    assert cleaned == ["generator-closed"]


def test_sync_generator_runs_in_worker_thread():
    """Sync SSE generators run in a thread pool, not on the event loop."""
    app = FastAPI()
    loop_threads: list[int] = []
    gen_threads: list[int] = []

    @app.middleware("http")
    async def record_loop_thread(request: Any, call_next: Any) -> Any:
        loop_threads.append(threading.get_ident())
        return await call_next(request)

    @app.get("/sync", response_class=EventSourceResponse)
    def sync_stream() -> Iterable[dict[str, int]]:
        gen_threads.append(threading.get_ident())
        yield {"n": 1}
        yield {"n": 2}

    with TestClient(app) as client:
        response = client.get("/sync")
    assert response.status_code == 200
    assert len(gen_threads) == 1
    assert loop_threads
    assert gen_threads[0] != loop_threads[0]


def test_producer_exception_aborts_stream_and_propagates():
    """An exception in the endpoint generator is not swallowed: it
    propagates out of the response instead of ending the stream
    cleanly (and silently) after the last buffered event."""
    app = FastAPI()

    @app.get("/boom", response_class=EventSourceResponse)
    async def boom() -> AsyncIterable[dict[str, int]]:
        yield {"n": 1}
        raise RuntimeError("producer exploded")

    with TestClient(app) as client:
        with pytest.raises(RuntimeError, match="producer exploded"):
            client.get("/boom")


def test_serialization_error_propagates():
    """ResponseValidationError from a bad stream item is not swallowed."""
    app = FastAPI()

    @app.get("/invalid", response_class=EventSourceResponse)
    async def invalid() -> AsyncIterable[Item]:
        yield {"wrong": "shape"}  # type: ignore[misc]

    with TestClient(app) as client:
        with pytest.raises(ResponseValidationError):
            client.get("/invalid")


def test_producer_exception_closes_generator_and_dependencies():
    """Generator cleanup and yield dependencies exit even when the
    producer fails mid-stream."""
    events: list[str] = []
    app = FastAPI()

    async def get_db() -> AsyncIterator[str]:
        events.append("db-enter")
        try:
            yield "db"
        finally:
            events.append("db-exit")

    @app.get("/boom-dep", response_class=EventSourceResponse)
    async def boom_dep(db: str = Depends(get_db)) -> AsyncIterable[dict[str, int]]:
        try:
            yield {"n": 1}
            raise RuntimeError("mid-stream failure")
        finally:
            events.append("generator-closed")

    with TestClient(app) as client:
        with pytest.raises(RuntimeError, match="mid-stream failure"):
            client.get("/boom-dep")
    assert events == ["db-enter", "generator-closed", "db-exit"]


def test_background_task_runs_after_stream():
    """Background tasks still run after the SSE stream completes."""
    ran: list[str] = []
    app = FastAPI()

    @app.get("/bg", response_class=EventSourceResponse)
    async def bg(background_tasks: BackgroundTasks) -> AsyncIterable[dict[str, int]]:
        background_tasks.add_task(ran.append, "bg-ran")
        yield {"n": 1}

    with TestClient(app) as client:
        response = client.get("/bg")
    assert response.status_code == 200
    assert ran == ["bg-ran"]


def test_sse_custom_status_code():
    app = FastAPI()

    @app.post("/created", response_class=EventSourceResponse, status_code=201)
    async def created() -> AsyncIterable[dict[str, bool]]:
        yield {"ok": True}

    with TestClient(app) as client:
        response = client.post("/created")
    assert response.status_code == 201
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert 'data: {"ok":true}' in response.text


def test_custom_media_type_response_class():
    """A subclass of EventSourceResponse with a custom media type is
    honored, while the body still uses the SSE wire format."""

    class CustomEventStreamResponse(EventSourceResponse):
        media_type = "text/x-custom-event-stream"

    app = FastAPI()

    @app.get("/custom", response_class=CustomEventStreamResponse)
    async def custom() -> AsyncIterable[dict[str, int]]:
        yield {"n": 1}

    with TestClient(app) as client:
        response = client.get("/custom")
    assert response.status_code == 200
    assert (
        response.headers["content-type"] == "text/x-custom-event-stream; charset=utf-8"
    )
    assert 'data: {"n":1}' in response.text


def test_last_event_id_not_replayed_automatically():
    """FastAPI does not auto-resume streams: Last-Event-ID is only a
    header. Unless the app reads it, the full stream is sent again
    (no automatic re-consumption of potentially non-replayable sources)."""
    app = FastAPI()

    @app.get("/events", response_class=EventSourceResponse)
    async def events() -> AsyncIterable[ServerSentEvent]:
        yield ServerSentEvent(data="a", id="1")
        yield ServerSentEvent(data="b", id="2")

    with TestClient(app) as client:
        response = client.get("/events", headers={"Last-Event-ID": "1"})
    assert response.status_code == 200
    assert 'data: "a"' in response.text
    assert 'data: "b"' in response.text


def test_empty_event_serialization():
    """An event with no fields is a valid (ignored) blank dispatch."""
    assert format_sse_event() == b"\n"


def test_empty_event_over_endpoint():
    app = FastAPI()

    @app.get("/empty", response_class=EventSourceResponse)
    async def empty() -> AsyncIterable[ServerSentEvent]:
        yield ServerSentEvent()
        yield ServerSentEvent(data="after")

    with TestClient(app) as client:
        response = client.get("/empty")
    assert response.status_code == 200
    assert 'data: "after"' in response.text


def test_field_order_in_serialized_event():
    """Fields follow the wire order: comment, event, data, id, retry."""
    assert (
        format_sse_event(data_str="x", event="e", id="1", retry=100, comment="c")
        == b": c\nevent: e\ndata: x\nid: 1\nretry: 100\n\n"
    )


def test_unicode_data_round_trip():
    """Non-ASCII data survives the wire format (JSON-escaped or raw UTF-8)."""
    app = FastAPI()

    @app.get("/unicode", response_class=EventSourceResponse)
    async def unicode_events() -> AsyncIterable[ServerSentEvent]:
        yield ServerSentEvent(data="héllo wörld ✨")
        yield ServerSentEvent(raw_data="héllo ✨")
        yield ServerSentEvent(comment="ping ✨")

    with TestClient(app) as client:
        response = client.get("/unicode")
    assert response.status_code == 200
    data_lines = [
        line.removeprefix("data: ")
        for line in response.text.split("\n")
        if line.startswith("data: ")
    ]
    # JSON-encoded data is valid JSON and round-trips to the original.
    assert json.loads(data_lines[0]) == "héllo wörld ✨"
    # raw_data goes on the wire unescaped (UTF-8 encoded).
    assert data_lines[1] == "héllo ✨"
    assert ": ping ✨" in response.text


def test_multiline_raw_data_over_endpoint():
    """Multi-line data is split into one `data:` field per line."""
    app = FastAPI()

    @app.get("/multiline", response_class=EventSourceResponse)
    async def multiline() -> AsyncIterable[ServerSentEvent]:
        yield ServerSentEvent(raw_data="line1\nline2\r\nline3")

    with TestClient(app) as client:
        response = client.get("/multiline")
    assert "data: line1\ndata: line2\ndata: line3\n" in response.text


def test_finite_stream_completes_cleanly():
    """A finite generator ends the stream with a proper final chunk."""
    app = FastAPI()

    @app.get("/finite", response_class=EventSourceResponse)
    async def finite() -> AsyncIterable[dict[str, int]]:
        for n in range(3):
            yield {"n": n}

    async def main() -> DisconnectHarness:
        harness = DisconnectHarness(disconnect_after=10**9)
        await app(make_scope("/finite"), harness.receive, harness.send)
        return harness

    harness = anyio.run(main)
    body_messages = [
        message
        for message in harness.sent_messages
        if message["type"] == "http.response.body"
    ]
    # 3 event chunks + final empty chunk with more_body=False.
    assert len(body_messages) == 4
    assert body_messages[-1]["more_body"] is False
    assert harness.body_bytes.count(b"data: ") == 3
