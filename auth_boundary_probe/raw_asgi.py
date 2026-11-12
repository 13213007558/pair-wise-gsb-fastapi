"""In-process ASGI probe that records the raw emitted events.

TestClient converts ASGI events into Python exceptions, which hides what was
actually put on the wire.  This driver calls the app as a raw ASGI3 application
so every sent message (and any exception escaping the middleware stack) is
captured verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import anyio

from starlette.types import ASGIApp, Message, Scope


@dataclass
class RawResult:
    events: list[Message] = field(default_factory=list)
    escaped_exc: BaseException | None = None
    task_cancelled: bool = False

    @property
    def event_types(self) -> list[str]:
        return [event["type"] for event in self.events]

    def summary(self) -> dict[str, Any]:
        first = self.events[0] if self.events else None
        close_code = None
        http_status = None
        if first and first["type"] == "websocket.close":
            close_code = first.get("code")
        if first and first["type"] in ("websocket.http.response.start", "http.response.start"):
            http_status = first.get("status")
        return {
            "events": self.event_types,
            "close_code": close_code,
            "http_status": http_status,
            "escaped_exc": type(self.escaped_exc).__name__ if self.escaped_exc else None,
        }


def _ws_scope(path: str, query: str, *, denial_extension: bool) -> Scope:
    extensions: dict[str, dict] = {}
    if denial_extension:
        extensions["websocket.http.response"] = {}
    return {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": [(b"host", b"testserver"), (b"sec-websocket-version", b"13")],
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "subprotocols": [],
        "extensions": extensions,
    }


def _http_scope(path: str, query: str) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": [(b"host", b"testserver")],
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
    }


async def drive(app: ASGIApp, scope: Scope, messages: list[Message]) -> RawResult:
    result = RawResult()
    send_tx, send_rx = anyio.create_memory_object_stream[Message](100)
    receive_tx, receive_rx = anyio.create_memory_object_stream[Message](100)

    async def capture_send(message: Message) -> None:
        result.events.append(message)
        await send_tx.send(message)

    async def scripted_receive() -> Message:
        return await receive_rx.receive()

    async with send_tx, send_rx, receive_tx, receive_rx:
        for message in messages:
            await receive_tx.send(message)
        try:
            with anyio.CancelScope() as scope_guard:
                await app(scope, scripted_receive, capture_send)
        except BaseException as exc:  # noqa: BLE001 - we want everything, incl. RuntimeError
            result.escaped_exc = exc
        result.task_cancelled = scope_guard.cancel_called
    return result


async def drive_websocket(
    app: ASGIApp, path: str, query: str, *, denial_extension: bool = True
) -> RawResult:
    scope = _ws_scope(path, query, denial_extension=denial_extension)
    # Client sends connect, then waits; app either closes/denials or accepts.
    messages = [{"type": "websocket.connect"}]
    return await drive(app, scope, messages)


class StrictASGIServerError(RuntimeError):
    """A spec-compliant server rejected an event for the negotiated scope."""


def strict_server_send(scope: Scope, capture):
    """Mimic a server that enforces the ASGI websocket event protocol.

    The ``websocket.http.response.*`` messages are only legal when the scope
    advertises the ``websocket.http.response`` extension.  Starlette's own
    Response does not check this, so a compliant server is the one that has to
    raise.  uvicorn's implementation rejects the event at this boundary.
    """

    async def send(message: Message) -> None:
        if message["type"].startswith("websocket.http.response.") and (
            "websocket.http.response" not in scope.get("extensions", {})
        ):
            raise StrictASGIServerError(
                f"server got {message['type']!r} but scope does not advertise "
                "the 'websocket.http.response' extension"
            )
        await capture(message)

    return send


async def drive_websocket_strict(
    app: ASGIApp, path: str, query: str, *, denial_extension: bool
) -> RawResult:
    """Drive the app behind a spec-enforcing server send callable."""
    scope = _ws_scope(path, query, denial_extension=denial_extension)
    result = RawResult()
    send_tx, send_rx = anyio.create_memory_object_stream[Message](100)
    receive_tx, receive_rx = anyio.create_memory_object_stream[Message](100)

    async def capture_send(message: Message) -> None:
        result.events.append(message)
        await send_tx.send(message)

    guarded_send = strict_server_send(scope, capture_send)

    async def scripted_receive() -> Message:
        return await receive_rx.receive()

    async with send_tx, send_rx, receive_tx, receive_rx:
        await receive_tx.send({"type": "websocket.connect"})
        try:
            with anyio.CancelScope() as scope_guard:
                await app(scope, scripted_receive, guarded_send)
        except BaseException as exc:  # noqa: BLE001
            result.escaped_exc = exc
        result.task_cancelled = scope_guard.cancel_called
    return result


async def drive_http(app: ASGIApp, path: str, query: str) -> RawResult:
    scope = _http_scope(path, query)
    messages = [{"type": "http.request", "body": b"", "more_body": False}]
    return await drive(app, scope, messages)
