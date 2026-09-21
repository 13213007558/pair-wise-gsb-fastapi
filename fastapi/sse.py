import json
from collections.abc import AsyncIterable, Callable, Iterable, Mapping
from typing import Annotated, Any

import anyio
from annotated_doc import Doc
from fastapi.encoders import jsonable_encoder
from pydantic import AfterValidator, BaseModel, Field, model_validator
from starlette.requests import ClientDisconnect
from starlette.responses import BackgroundTask, StreamingResponse
from starlette.types import Receive, Scope, Send

# Canonical SSE event schema matching the OpenAPI 3.2 spec
# (Section 4.14.4 "Special Considerations for Server-Sent Events")
_SSE_EVENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "data": {"type": "string"},
        "event": {"type": "string"},
        "id": {"type": "string"},
        "retry": {"type": "integer", "minimum": 0},
    },
}

# Keep-alive comment, per the SSE spec recommendation
KEEPALIVE_COMMENT = b": ping\n\n"

# Seconds between keep-alive pings when a generator is idle.
# Private but importable so tests can monkeypatch it.
_PING_INTERVAL: float = 15.0

# Maximum number of serialized events buffered between the producer and the
# network send. When the client cannot keep up, the producer blocks here
# instead of growing an unbounded queue.
_DEFAULT_MAX_BUFFER_SIZE: int = 16


def _check_single_line(v: str | None, field_name: str) -> str | None:
    if v is not None and ("\r" in v or "\n" in v):
        raise ValueError(f"SSE '{field_name}' must be a single line")
    return v


def _check_event_single_line(v: str | None) -> str | None:
    return _check_single_line(v, "event")


def _check_id_valid(v: str | None) -> str | None:
    if v is not None and "\0" in v:
        raise ValueError("SSE 'id' must not contain null characters")
    return _check_single_line(v, "id")


class ServerSentEvent(BaseModel):
    """Represents a single Server-Sent Event.

    When `yield`ed from a *path operation function* that uses
    `response_class=EventSourceResponse`, each `ServerSentEvent` is encoded
    into the [SSE wire format](https://html.spec.whatwg.org/multipage/server-sent-events.html#parsing-an-event-stream)
    (`text/event-stream`).

    If you yield a plain object (dict, Pydantic model, etc.) instead, it is
    automatically JSON-encoded and sent as the `data:` field.

    All `data` values **including plain strings** are JSON-serialized.

    For example, `data="hello"` produces `data: "hello"` on the wire (with
    quotes).
    """

    data: Annotated[
        Any,
        Doc(
            """
            The event payload.

            Can be any JSON-serializable value: a Pydantic model, dict, list,
            string, number, etc. It is **always** serialized to JSON: strings
            are quoted (`"hello"` becomes `data: "hello"` on the wire).

            Mutually exclusive with `raw_data`.
            """
        ),
    ] = None
    raw_data: Annotated[
        str | None,
        Doc(
            """
            Raw string to send as the `data:` field **without** JSON encoding.

            Use this when you need to send pre-formatted text, HTML fragments,
            CSV lines, or any non-JSON payload. The string is placed directly
            into the `data:` field as-is.

            Mutually exclusive with `data`.
            """
        ),
    ] = None
    event: Annotated[
        str | None,
        AfterValidator(_check_event_single_line),
        Doc(
            """
            Optional event type name.

            Maps to `addEventListener(event, ...)` on the browser. When omitted,
            the browser dispatches on the generic `message` event. Must be a
            single line.
            """
        ),
    ] = None
    id: Annotated[
        str | None,
        AfterValidator(_check_id_valid),
        Doc(
            """
            Optional event ID.

            The browser sends this value back as the `Last-Event-ID` header on
            automatic reconnection. **Must be a single line** and must not contain
            null (`\0`) characters.
            """
        ),
    ] = None
    retry: Annotated[
        int | None,
        Field(ge=0),
        Doc(
            """
            Optional reconnection time in **milliseconds**.

            Tells the browser how long to wait before reconnecting after the
            connection is lost. Must be a non-negative integer.
            """
        ),
    ] = None
    comment: Annotated[
        str | None,
        Doc(
            """
            Optional comment line(s).

            Comment lines start with `:` in the SSE wire format and are ignored by
            `EventSource` clients. Useful for keep-alive pings to prevent
            proxy/load-balancer timeouts.
            """
        ),
    ] = None

    @model_validator(mode="after")
    def _check_data_exclusive(self) -> "ServerSentEvent":
        if self.data is not None and self.raw_data is not None:
            raise ValueError(
                "Cannot set both 'data' and 'raw_data' on the same "
                "ServerSentEvent. Use 'data' for JSON-serialized payloads "
                "or 'raw_data' for pre-formatted strings."
            )
        return self


def _split_sse_lines(value: str) -> list[str]:
    # Split on SSE-spec line terminators only (\n, \r\n, \r), preserving
    # trailing empty strings.
    return value.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def format_sse_event(
    *,
    data_str: Annotated[
        str | None,
        Doc(
            """
            Pre-serialized data string to use as the `data:` field.
            """
        ),
    ] = None,
    event: Annotated[
        str | None,
        Doc(
            """
            Optional event type name (`event:` field).
            """
        ),
    ] = None,
    id: Annotated[
        str | None,
        Doc(
            """
            Optional event ID (`id:` field).
            """
        ),
    ] = None,
    retry: Annotated[
        int | None,
        Doc(
            """
            Optional reconnection time in milliseconds (`retry:` field).
            """
        ),
    ] = None,
    comment: Annotated[
        str | None,
        Doc(
            """
            Optional comment line(s) (`:` prefix).
            """
        ),
    ] = None,
) -> bytes:
    """Build SSE wire-format bytes from **pre-serialized** data.

    Multi-line strings (splitting on ``\n``, ``\r`` and ``\r\n``) produce
    repeated field lines, non-ASCII text is UTF-8 encoded, and the result
    always ends with ``\n\n`` (the event terminator).
    """
    lines: list[str] = []

    if comment is not None:
        for line in _split_sse_lines(comment):
            lines.append(f": {line}")

    if event is not None:
        lines.append(f"event: {event}")

    if data_str is not None:
        for line in _split_sse_lines(data_str):
            lines.append(f"data: {line}")

    if id is not None:
        lines.append(f"id: {id}")

    if retry is not None:
        lines.append(f"retry: {retry}")

    lines.append("")
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def encode_server_sent_event(event: ServerSentEvent) -> bytes:
    """Serialize one `ServerSentEvent` (all SSE fields) to wire bytes."""
    if event.raw_data is not None:
        data_str: str | None = event.raw_data
    elif event.data is not None:
        if hasattr(event.data, "model_dump_json"):
            data_str = event.data.model_dump_json()
        else:
            data_str = json.dumps(jsonable_encoder(event.data), ensure_ascii=False)
    else:
        data_str = None
    return format_sse_event(
        data_str=data_str,
        event=event.event,
        id=event.id,
        retry=event.retry,
        comment=event.comment,
    )


def default_encode_sse_event(item: Any) -> bytes:
    """Default per-item encoder used by `EventSourceResponse`.

    `ServerSentEvent` instances are rendered with all their SSE fields;
    already-serialized `bytes`/`memoryview` are forwarded untouched; every
    other value (including plain `str`) is JSON-encoded and wrapped in a
    `data:` field.
    """
    if isinstance(item, ServerSentEvent):
        return encode_server_sent_event(item)
    if isinstance(item, memoryview):
        return item.tobytes()
    if isinstance(item, bytes):
        return item
    if hasattr(item, "model_dump_json"):
        data_str = item.model_dump_json()
    else:
        data_str = json.dumps(jsonable_encoder(item), ensure_ascii=False)
    return format_sse_event(data_str=data_str)


class EventSourceResponse(StreamingResponse):
    """Streaming response with `text/event-stream` media type.

    Use as `response_class=EventSourceResponse` on a *path operation* that
    uses `yield` to enable Server-Sent Events (SSE) responses. Works with
    **any HTTP method** (`GET`, `POST`, etc.), which makes it compatible
    with protocols like MCP that stream SSE over `POST`.

    Items from *content* are turned into SSE wire bytes through a bounded
    producer/consumer pipeline:

    * The generator is iterated by a separate **producer** task that pushes
      serialized events into a bounded memory stream. At most
      *max_buffer_size* events (plus the event currently being sent) are
      ever buffered, so a slow client or a blocked downstream `send` exerts
      backpressure on the producer instead of growing an unbounded queue.
    * On client disconnect (or a failed `send`) the internal task group is
      cancelled: the producer is cancelled, async generators receive
      `GeneratorExit` so `try/finally` blocks and dependencies using `yield`
      finalize promptly, and no further events are produced or buffered.
    * When *ping* is not `None`, a `: ping` comment is emitted whenever the
      producer has been idle for that many seconds. Ping emission shares the
      consumer side of the channel, so pings are never queued ahead of a
      blocked `send`.
    * An exception raised by the generator or the item encoder propagates
      out of the response instead of being swallowed. Background tasks only
      run after the whole stream has been sent successfully.

    Synchronous generators are iterated in a worker thread (through
    `starlette.concurrency.iterate_in_threadpool`). A blocking synchronous
    generator cannot be interrupted mid-iteration, so after a disconnect it
    is stopped at its next iteration checkpoint, once the current blocking
    step returns.

    The server never interprets the `Last-Event-ID` header: reconnection is
    entirely under the application's control. Read the header explicitly and
    replay events only from sources your application can rewind; never
    re-consume non-replayable sources automatically.
    """

    media_type = "text/event-stream"

    def __init__(
        self,
        content: AsyncIterable[Any] | Iterable[Any],
        *,
        ping: float | None = _PING_INTERVAL,
        max_buffer_size: int = _DEFAULT_MAX_BUFFER_SIZE,
        encode: Callable[[Any], bytes] = default_encode_sse_event,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        media_type: str | None = None,
        background: BackgroundTask | None = None,
    ) -> None:
        if max_buffer_size < 0:
            raise ValueError("max_buffer_size must be a non-negative integer")
        if ping is not None and ping <= 0:
            raise ValueError("ping must be a positive number or None")
        super().__init__(
            content,
            status_code=status_code,
            headers=headers,
            media_type=media_type,
            background=background,
        )
        self.ping_interval = ping
        self.max_buffer_size = max_buffer_size
        self._encode_item = encode
        self._receive: Receive | None = None
        self._stream_completed = False

    async def listen_for_disconnect(self, receive: Receive) -> None:
        while True:
            message = await receive()
            if message["type"] == "http.request" and not message.get("more_body"):
                continue
            if message["type"] == "http.disconnect":
                return

    async def _produce(
        self, send_stream: anyio.abc.ObjectSendStream[bytes]
    ) -> None:
        # Iteration is cancellable so disconnect/backpressure stops the
        # endpoint at its next await; shutdown (aclose) is shielded so the
        # endpoint's `finally` blocks can release resources acquired by
        # `yield` dependencies even though the surrounding task group is
        # already cancelled. Without the shield, awaits inside those blocks
        # would be aborted immediately, postponing real cleanup to GC.
        try:
            async with send_stream:
                async for item in self.body_iterator:
                    await send_stream.send(self._encode_item(item))
        finally:
            with anyio.CancelScope(shield=True):
                aclose = getattr(self.body_iterator, "aclose", None)
                if aclose is not None:
                    await aclose()

    async def _pump(
        self,
        receive_stream: anyio.abc.ObjectReceiveStream[bytes],
        send: Send,
    ) -> bool:
        """Forward events to the client, inserting keepalive comments.

        Runs on the consumer side of the bounded channel: a blocked `send`
        stops receiving, which in turn blocks the producer.
        """
        completed = False
        async with receive_stream:
            while True:
                if self.ping_interval is not None:
                    try:
                        with anyio.fail_after(self.ping_interval):
                            chunk = await receive_stream.receive()
                    except TimeoutError:
                        chunk = KEEPALIVE_COMMENT
                    except anyio.EndOfStream:
                        break
                else:
                    try:
                        chunk = await receive_stream.receive()
                    except anyio.EndOfStream:
                        break
                if not isinstance(chunk, bytes | memoryview):
                    chunk = chunk.encode(self.charset)
                await send(
                    {
                        "type": "http.response.body",
                        "body": chunk,
                        "more_body": True,
                    }
                )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"",
                    "more_body": False,
                }
            )
            completed = True
        return completed

    async def stream_response(self, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": self.raw_headers,
            }
        )
        send_stream, receive_stream = anyio.create_memory_object_stream[bytes](
            max_buffer_size=self.max_buffer_size
        )
        if self._receive is None:  # pragma: no cover - defensive
            raise RuntimeError(
                "EventSourceResponse was not called as an ASGI application"
        )
        receive = self._receive
        send_failed = False
        async with anyio.create_task_group() as task_group:
            async def _watch_disconnect() -> None:
                await self.listen_for_disconnect(receive)
                # Client gone: tear down producer and pump immediately.
                task_group.cancel_scope.cancel()

            # Producer feeds the bounded channel and exits when the
            # generator is exhausted (or raises). The pump drains the
            # channel, so the normal shutdown sequence is: producer
            # done -> pump flushes all buffered events -> pump done ->
            # cancel the disconnect watcher. A client disconnect (or a
            # failed send) cancels the whole group instead, stopping the
            # producer at its next checkpoint and finalizing async
            # generators promptly.
            task_group.start_soon(_watch_disconnect)
            task_group.start_soon(self._produce, send_stream)

            try:
                self._stream_completed = await self._pump(
                    receive_stream, send
                )
            except OSError:
                # Failed downstream write: the client is gone. Cancel the
                # rest of the pipeline and finalize the generator, then
                # surface ClientDisconnect once the group has settled.
                send_failed = True
                task_group.cancel_scope.cancel()
                with anyio.CancelScope(shield=True):
                    aclose = getattr(self.body_iterator, "aclose", None)
                    if aclose is not None:
                        await aclose()
            finally:
                task_group.cancel_scope.cancel()
        if send_failed:
            raise ClientDisconnect()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self._receive = receive
        if scope["type"] == "websocket":
            send = self._wrap_websocket_denial_send(send)
            await self.stream_response(send)
            if self.background is not None and self._stream_completed:
                await self.background()
            return
        await self.stream_response(send)
        if self.background is not None and self._stream_completed:
            await self.background()
