"""Tests for `EventSourceResponse` lifecycle semantics.

Covers the bounded channel (backpressure under slow `send`), client
disconnect cancellation, async-generator and `yield`-dependency shutdown,
producer/send error propagation, background task handling, synchronous
generator thread ownership, and application-controlled `Last-Event-ID`.
"""

import threading
from collections.abc import AsyncIterable, Iterable
from typing import Annotated

import anyio
import pytest
from fastapi import Depends, FastAPI, Header
from fastapi.sse import EventSourceResponse, ServerSentEvent
from starlette.requests import ClientDisconnect
from starlette.types import Message, Scope

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning"),
]

SCOPE: Scope = {
    "type": "http",
    "asgi": {"version": "3.0", "spec_version": "2.3"},
    "http_version": "1.1",
    "method": "GET",
    "path": "/stream",
    "query_string": b"",
    "root_path": "",
    "headers": [],
    "server": ("test", 80),
}


async def _drive(
    app: FastAPI,
    *,
    send,
    receive,
    timeout: float = 5.0,
) -> None:
    with anyio.move_on_after(timeout) as cancel_scope:
        await app(SCOPE, receive, send)
    assert not cancel_scope.cancel_called, "SSE pipeline hung (did not finish)"


def _disconnecting_receive(after: float):
    calls = {"n": 0}

    async def receive() -> Message:
        calls["n"] += 1
        if calls["n"] == 1:
            return {"type": "http.request", "body": b""}
        await anyio.sleep(after)
        return {"type": "http.disconnect"}

    return receive


async def test_bounded_channel_backpressure() -> None:
    """A blocked send cannot make the producer run away.

    With max_buffer_size=4 the producer may create at most 5 events
    (4 buffered + the one being handed to the pump) while the client send
    is stalled.
    """
    produced = {"n": 0}

    async def infinite() -> AsyncIterable[dict]:
        i = 0
        while True:
            yield {"n": i}
            i += 1
            produced["n"] = i

    gate = anyio.Event()

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            await gate.wait()

    response = EventSourceResponse(infinite(), ping=None, max_buffer_size=4)
    with anyio.move_on_after(5):
        await response(SCOPE, _disconnecting_receive(after=0.4), send)
    assert produced["n"] <= 5, produced["n"]


async def test_event_order_preserved() -> None:
    """Events reach the wire in the exact order yielded by the source."""
    bodies: list[bytes] = []

    async def source() -> AsyncIterable[dict]:
        for i in range(50):
            yield {"n": i}

    async def receive() -> Message:
        await anyio.sleep_forever()
        return {"type": "http.disconnect"}  # pragma: no cover

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            bodies.append(message.get("body", b""))

    response = EventSourceResponse(source(), ping=None)
    with anyio.move_on_after(5):
        await response(SCOPE, receive, send)

    events = [b for b in bodies if b]
    assert events == [f'data: {{"n": {i}}}\n\n'.encode() for i in range(50)]
    assert bodies[-1] == b""  # terminating empty body


async def test_disconnect_cancels_generator_and_dependencies() -> None:
    state = {"gen": False, "dep": False}

    async def tracking_dependency():
        try:
            yield
        finally:
            await anyio.sleep(0.01)
            state["dep"] = True

    app = FastAPI()

    @app.get("/stream", response_class=EventSourceResponse)
    async def stream(_: None = Depends(tracking_dependency)) -> AsyncIterable[dict]:
        i = 0
        try:
            while True:
                yield {"n": i}
                i += 1
        finally:
            await anyio.sleep(0.01)
            state["gen"] = True

    gate = anyio.Event()

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            await gate.wait()

    await _drive(app, send=send, receive=_disconnecting_receive(after=0.3))
    assert state["gen"] is True
    assert state["dep"] is True


async def test_finite_stream_drains_and_closes() -> None:
    state = {"gen": False}
    bodies: list[bytes] = []

    async def finite() -> AsyncIterable[dict]:
        try:
            for i in range(5):
                yield {"n": i}
        finally:
            state["gen"] = True

    async def receive() -> Message:
        await anyio.sleep_forever()
        return {"type": "http.disconnect"}  # pragma: no cover

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            bodies.append(message.get("body", b""))

    response = EventSourceResponse(finite(), ping=None)
    with anyio.move_on_after(5):
        await response(SCOPE, receive, send)
    assert state["gen"] is True
    assert b"".join(bodies).count(b"data: ") == 5
    assert bodies[-1] == b""


async def test_producer_exception_propagates() -> None:
    async def boom() -> AsyncIterable[dict]:
        yield {"ok": True}
        raise RuntimeError("producer exploded")

    sent: list[Message] = []

    async def receive() -> Message:
        await anyio.sleep_forever()
        return {"type": "http.disconnect"}  # pragma: no cover

    async def send(message: Message) -> None:
        sent.append(message)

    response = EventSourceResponse(boom(), ping=None)
    with pytest.raises(BaseException):
        with anyio.move_on_after(5):
            await response(SCOPE, receive, send)
    # The event produced before the failure was still delivered.
    assert any(
        message.get("body") == b'data: {"ok": true}\n\n'
        for message in sent
        if message["type"] == "http.response.body"
    )


async def test_send_failure_cancels_pipeline() -> None:
    """A downstream `send` failure tears down the producer just like a
    disconnect: the endpoint generator is finalized and nothing else is
    produced.
    """
    state = {"gen": False, "produced": 0}

    async def source() -> AsyncIterable[dict]:
        i = 0
        try:
            while True:
                yield {"n": i}
                i += 1
                state["produced"] = i
        finally:
            state["gen"] = True

    calls = {"n": 0}

    async def receive() -> Message:
        await anyio.sleep_forever()
        return {"type": "http.disconnect"}  # pragma: no cover

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            calls["n"] += 1
            if calls["n"] == 1:
                raise BrokenPipeError("connection gone")

    response = EventSourceResponse(source(), ping=None)
    with anyio.move_on_after(5):
        try:
            await response(SCOPE, receive, send)
            assert False, "send failure must surface as ClientDisconnect"
        except ClientDisconnect:
            pass
    assert state["gen"] is True


async def test_background_task_runs_only_after_full_stream() -> None:
    order: list[str] = []

    async def source() -> AsyncIterable[dict]:
        for i in range(3):
            order.append("event")
            yield {"n": i}

    async def background() -> None:
        order.append("background")

    async def receive() -> Message:
        await anyio.sleep_forever()
        return {"type": "http.disconnect"}  # pragma: no cover

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            order.append("send")

    response = EventSourceResponse(source(), ping=None, background=background)
    with anyio.move_on_after(5):
        await response(SCOPE, receive, send)
    # The bounded channel lets the producer run ahead of the network, so
    # events may be produced before previous sends complete; the invariant
    # is that the background task runs only after every send is done.
    assert order.count("event") == 3
    assert order.count("send") == 3
    assert order[-1] == "background"


async def test_background_task_skipped_on_disconnect() -> None:
    ran = {"bg": False}
    gate = anyio.Event()

    async def infinite() -> AsyncIterable[dict]:
        i = 0
        while True:
            yield {"n": i}
            i += 1

    async def background() -> None:
        ran["bg"] = True

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            await gate.wait()

    response = EventSourceResponse(
        infinite(), ping=None, background=background
    )
    with anyio.move_on_after(5):
        await response(SCOPE, _disconnecting_receive(after=0.3), send)
    assert ran["bg"] is False


async def test_sync_generator_runs_off_event_loop_thread() -> None:
    """Synchronous generators are iterated in a worker thread."""
    loop_thread = threading.get_ident()
    seen_threads: set[int] = set()

    def source() -> Iterable[dict]:
        for i in range(5):
            seen_threads.add(threading.get_ident())
            yield {"n": i}

    bodies: list[bytes] = []

    async def receive() -> Message:
        await anyio.sleep_forever()
        return {"type": "http.disconnect"}  # pragma: no cover

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            bodies.append(message.get("body", b""))

    response = EventSourceResponse(source(), ping=None)
    with anyio.move_on_after(5):
        await response(SCOPE, receive, send)
    assert seen_threads
    assert loop_thread not in seen_threads
    assert b"".join(bodies).count(b"data: ") == 5


async def test_sync_generator_cancelled_at_checkpoint_on_disconnect() -> None:
    """A blocking sync generator cannot be interrupted mid-iteration, but it
    is stopped once its current blocking step returns (next checkpoint).
    """
    state = {"closed": False, "produced": 0}
    event = threading.Event()
    after_cancel = anyio.Event()

    def source() -> Iterable[dict]:
        try:
            i = 0
            while True:
                yield {"n": i}
                i += 1
                state["produced"] = i
                if i == 1:
                    # Block the worker thread until the disconnect happened.
                    event.wait(2.0)
        finally:
            state["closed"] = True

    gate = anyio.Event()

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            # First body arrives; let the disconnect/cancel happen while the
            # generator's worker thread is blocked, then release it.
            await after_cancel.wait()

    response = EventSourceResponse(source(), ping=None, max_buffer_size=4)

    async def runner() -> None:
        with anyio.move_on_after(5):
            await response(SCOPE, _disconnecting_receive(after=0.2), send)
        event.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(runner)
        await anyio.sleep(0.4)
        after_cancel.set()
    assert state["closed"] is True
    assert state["produced"] <= 2


async def test_last_event_id_is_left_to_the_application() -> None:
    """FastAPI never auto-replays based on `Last-Event-ID`; the endpoint can
    read the header and decide the reconnection strategy itself.
    """
    seen: list[str | None] = []
    app = FastAPI()

    @app.get("/stream", response_class=EventSourceResponse)
    async def stream(
        last_event_id: Annotated[str | None, Header()] = None,
    ) -> AsyncIterable[ServerSentEvent]:
        seen.append(last_event_id)
        for i in range(2):
            yield ServerSentEvent(data={"n": i}, id=str(i))

    bodies: list[bytes] = []

    def make_receive():
        called = {"n": 0}

        async def receive() -> Message:
            called["n"] += 1
            if called["n"] == 1:
                return {"type": "http.request", "body": b""}
            await anyio.sleep_forever()
            return {"type": "http.disconnect"}  # pragma: no cover

        return receive

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            bodies.append(message.get("body", b""))

    scope = {**SCOPE, "headers": [(b"last-event-id", b"42")]}
    with anyio.move_on_after(5):
        await app(scope, make_receive(), send)
    assert seen == ["42"]
    wire = b"".join(bodies)
    # All events are still sent; no automatic dedup/replay happened.
    assert wire.count(b"id: 0") == 1
    assert wire.count(b"id: 1") == 1


async def test_custom_media_type_subclass() -> None:
    """A subclass can override the media type while keeping SSE semantics."""

    class CustomSSE(EventSourceResponse):
        media_type = "application/x-ndjson-stream"

    started: list[dict] = []

    async def source() -> AsyncIterable[dict]:
        yield {"n": 1}

    async def receive() -> Message:
        await anyio.sleep_forever()
        return {"type": "http.disconnect"}  # pragma: no cover

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            started.append(message)

    response = CustomSSE(source(), ping=None)
    with anyio.move_on_after(5):
        await response(SCOPE, receive, send)
    headers = dict(started[0]["headers"])
    assert headers[b"content-type"] == b"application/x-ndjson-stream"
