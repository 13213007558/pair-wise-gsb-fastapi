import asyncio
import time
from collections.abc import AsyncGenerator, Generator
from typing import Annotated, Any

import pytest
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Security,
    WebSocket,
    WebSocketException,
    status,
)
from fastapi.security import APIKeyHeader
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

events: list[str] = []


def dep_sync() -> Generator[str, None, None]:
    events.append("enter dep_sync")
    try:
        yield "sync"
    finally:
        events.append("exit dep_sync")


async def dep_async() -> AsyncGenerator[str, None]:
    events.append("enter dep_async")
    try:
        yield "async"
    finally:
        events.append("exit dep_async")


async def dep_cleanup_fails() -> AsyncGenerator[str, None]:
    events.append("enter dep_cleanup_fails")
    try:
        yield "failing"
    finally:
        events.append("exit dep_cleanup_fails")
        raise ValueError("cleanup failed")


async def dep_cleanup_fails_ws_exc() -> AsyncGenerator[str, None]:
    events.append("enter dep_cleanup_fails_ws_exc")
    try:
        yield "failing"
    finally:
        events.append("exit dep_cleanup_fails_ws_exc")
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION, reason="late cleanup error"
        )


def dep_to_override() -> Generator[str, None, None]:
    events.append("enter dep_to_override")
    try:
        yield "original"
    finally:
        events.append("exit dep_to_override")


def dep_override() -> Generator[str, None, None]:
    events.append("enter dep_override")
    try:
        yield "override"
    finally:
        events.append("exit dep_override")


api_key_header = APIKeyHeader(name="X-Token")


def dep_function_scope() -> Generator[str, None, None]:
    events.append("enter dep_function_scope")
    try:
        yield "function"
    finally:
        events.append("exit dep_function_scope")


def dep_request_scope() -> Generator[str, None, None]:
    events.append("enter dep_request_scope")
    try:
        yield "request"
    finally:
        events.append("exit dep_request_scope")


FunctionDep = Annotated[str, Depends(dep_function_scope, scope="function")]
RequestDep = Annotated[str, Depends(dep_request_scope, scope="request")]

app = FastAPI()


@app.websocket("/reject-http")
async def ws_reject_http(
    websocket: WebSocket,
    sync: Annotated[str, Depends(dep_sync)],
    token: Annotated[str, Security(api_key_header)],
) -> None:  # pragma: no cover
    await websocket.accept()


@app.websocket("/reject-validation")
async def ws_reject_validation(
    websocket: WebSocket,
    sync: Annotated[str, Depends(dep_sync)],
    token: str,
) -> None:  # pragma: no cover
    await websocket.accept()


@app.websocket("/reject-ws-exception")
async def ws_reject_ws_exception(
    websocket: WebSocket, sync: Annotated[str, Depends(dep_sync)]
) -> None:
    raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="no token")


@app.websocket("/disconnect")
async def ws_disconnect(
    websocket: WebSocket,
    sync: Annotated[str, Depends(dep_sync)],
    asy: Annotated[str, Depends(dep_async)],
) -> None:
    await websocket.accept()
    await websocket.receive_text()


@app.websocket("/active-close")
async def ws_active_close(
    websocket: WebSocket,
    sync: Annotated[str, Depends(dep_sync)],
    asy: Annotated[str, Depends(dep_async)],
) -> None:
    await websocket.accept()
    await websocket.close(code=status.WS_1000_NORMAL_CLOSURE, reason="done")


@app.websocket("/active-close-cleanup-fails")
async def ws_active_close_cleanup_fails(
    websocket: WebSocket, failing: Annotated[str, Depends(dep_cleanup_fails)]
) -> None:
    await websocket.accept()
    await websocket.close(code=status.WS_1000_NORMAL_CLOSURE, reason="done")


@app.websocket("/active-close-cleanup-fails-ws-exc")
async def ws_active_close_cleanup_fails_ws_exc(
    websocket: WebSocket, failing: Annotated[str, Depends(dep_cleanup_fails_ws_exc)]
) -> None:
    await websocket.accept()
    await websocket.close(code=status.WS_1000_NORMAL_CLOSURE, reason="done")


@app.websocket("/cleanup-fails-open")
async def ws_cleanup_fails_open(
    websocket: WebSocket, failing: Annotated[str, Depends(dep_cleanup_fails)]
) -> None:
    await websocket.accept()


@app.websocket("/http-exc-after-accept")
async def ws_http_exc_after_accept(websocket: WebSocket) -> None:
    await websocket.accept()
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail="not allowed anymore"
    )


@app.websocket("/ws-exc-after-accept")
async def ws_ws_exc_after_accept(websocket: WebSocket) -> None:
    await websocket.accept()
    raise WebSocketException(code=status.WS_1003_UNSUPPORTED_DATA, reason="bad data")


@app.websocket("/background")
async def ws_background(
    websocket: WebSocket,
    background_tasks: BackgroundTasks,
    func: FunctionDep,
    req: RequestDep,
) -> None:
    background_tasks.add_task(lambda: events.append("background task"))
    await websocket.accept()
    await websocket.close()


@app.websocket("/override")
async def ws_override(
    websocket: WebSocket, value: Annotated[str, Depends(dep_to_override)]
) -> None:
    events.append(f"endpoint got {value}")
    await websocket.accept()
    await websocket.close()


@app.websocket("/cancel")
async def ws_cancel(
    websocket: WebSocket,
    sync: Annotated[str, Depends(dep_sync)],
    asy: Annotated[str, Depends(dep_async)],
) -> None:
    await websocket.accept()
    await asyncio.Event().wait()  # pragma: no cover


client = TestClient(app)


def wait_for_events(expected: list[str], timeout: float = 2.0) -> None:
    # The TestClient cancels the app task when the websocket session exits,
    # wait for the dependency cleanup to finish before exiting the session
    start = time.monotonic()
    while events != expected:
        if time.monotonic() - start > timeout:  # pragma: no cover
            raise AssertionError(f"expected events {expected}, got {events}")
        time.sleep(0.01)


async def run_ws_session(
    path: str, incoming: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], BaseException | None]:
    # Drive the ASGI app directly, to test the exact ASGI messages sent and
    # the exceptions raised, without the TestClient cancelling the app task
    scope: dict[str, Any] = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "ws",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "root_path": "",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "subprotocols": [],
    }
    sent_messages: list[dict[str, Any]] = []
    to_receive = list(incoming)

    async def receive() -> dict[str, Any]:
        if to_receive:
            return to_receive.pop(0)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover

    async def send(message: dict[str, Any]) -> None:
        sent_messages.append(message)

    exception: BaseException | None = None
    try:
        await app(scope, receive, send)
    except BaseException as e:
        exception = e
    return sent_messages, exception


@pytest.fixture(autouse=True)
def reset_events() -> Generator[None, None, None]:
    events.clear()
    yield
    app.dependency_overrides.clear()


def test_reject_before_handshake_with_http_exception() -> None:
    with pytest.raises(WebSocketDenialResponse) as exc_info:
        with client.websocket_connect("/reject-http"):
            pass  # pragma: no cover
    # The handshake is rejected with the HTTP response from the exception
    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert exc_info.value.json() == {"detail": "Not authenticated"}
    # Dependencies entered before the error are cleaned up exactly once
    assert events == ["enter dep_sync", "exit dep_sync"]


def test_reject_before_handshake_with_validation_error() -> None:
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/reject-validation"):
            pass  # pragma: no cover
    assert exc_info.value.code == status.WS_1008_POLICY_VIOLATION
    assert "token" in exc_info.value.reason
    assert "missing" in exc_info.value.reason
    assert events == ["enter dep_sync", "exit dep_sync"]


def test_reject_before_handshake_with_websocket_exception() -> None:
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/reject-ws-exception"):
            pass  # pragma: no cover
    assert exc_info.value.code == status.WS_1008_POLICY_VIOLATION
    assert exc_info.value.reason == "no token"
    assert events == ["enter dep_sync", "exit dep_sync"]


def test_disconnect_after_accept() -> None:
    with client.websocket_connect("/disconnect") as websocket:
        # The client disconnects right after the handshake, the endpoint gets
        # a WebSocketDisconnect and the app finishes cleanly
        websocket.close(code=1001, reason="going away")
        wait_for_events(
            [
                "enter dep_sync",
                "enter dep_async",
                "exit dep_async",
                "exit dep_sync",
            ]
        )
    # Both dependencies (sync and async generator) cleaned up exactly once
    assert events == [
        "enter dep_sync",
        "enter dep_async",
        "exit dep_async",
        "exit dep_sync",
    ]


def test_active_close() -> None:
    with client.websocket_connect("/active-close") as websocket:
        message = websocket.receive()
        wait_for_events(
            [
                "enter dep_sync",
                "enter dep_async",
                "exit dep_async",
                "exit dep_sync",
            ]
        )
    assert message["type"] == "websocket.close"
    assert message["code"] == status.WS_1000_NORMAL_CLOSURE
    assert message["reason"] == "done"
    assert events == [
        "enter dep_sync",
        "enter dep_async",
        "exit dep_async",
        "exit dep_sync",
    ]


def test_active_close_and_cleanup_raises() -> None:
    sent_messages, exception = asyncio.run(
        run_ws_session("/active-close-cleanup-fails", [{"type": "websocket.connect"}])
    )
    # The cleanup error is the only error, it propagates to the server
    assert isinstance(exception, ValueError)
    assert str(exception) == "cleanup failed"
    # The original close sent by the endpoint is preserved, no second close is
    # sent after the cleanup error
    close_messages = [m for m in sent_messages if m["type"] == "websocket.close"]
    assert len(close_messages) == 1
    assert close_messages[0]["code"] == status.WS_1000_NORMAL_CLOSURE
    assert close_messages[0]["reason"] == "done"
    # The cleanup error comes from the dependency exit, it ran exactly once
    assert events == ["enter dep_cleanup_fails", "exit dep_cleanup_fails"]


def test_active_close_and_cleanup_raises_websocket_exception() -> None:
    # A WebSocketException raised by the cleanup after the connection was
    # already closed must not trigger a second close
    sent_messages, exception = asyncio.run(
        run_ws_session(
            "/active-close-cleanup-fails-ws-exc", [{"type": "websocket.connect"}]
        )
    )
    assert exception is None
    close_messages = [m for m in sent_messages if m["type"] == "websocket.close"]
    assert len(close_messages) == 1
    assert close_messages[0]["code"] == status.WS_1000_NORMAL_CLOSURE
    assert close_messages[0]["reason"] == "done"
    assert events == [
        "enter dep_cleanup_fails_ws_exc",
        "exit dep_cleanup_fails_ws_exc",
    ]


def test_cleanup_raises_while_connected() -> None:
    sent_messages, exception = asyncio.run(
        run_ws_session("/cleanup-fails-open", [{"type": "websocket.connect"}])
    )
    assert isinstance(exception, ValueError)
    assert str(exception) == "cleanup failed"
    assert not any(m["type"] == "websocket.close" for m in sent_messages)
    assert events == ["enter dep_cleanup_fails", "exit dep_cleanup_fails"]


def test_http_exception_after_accept() -> None:
    with client.websocket_connect("/http-exc-after-accept") as websocket:
        message = websocket.receive()
        assert message["type"] == "websocket.close"
        assert message["code"] == status.WS_1008_POLICY_VIOLATION
        assert message["reason"] == "not allowed anymore"


def test_websocket_exception_after_accept() -> None:
    with client.websocket_connect("/ws-exc-after-accept") as websocket:
        message = websocket.receive()
        assert message["type"] == "websocket.close"
        assert message["code"] == status.WS_1003_UNSUPPORTED_DATA
        assert message["reason"] == "bad data"


def test_background_tasks_run_after_function_scope_cleanup() -> None:
    with client.websocket_connect("/background"):
        wait_for_events(
            [
                "enter dep_function_scope",
                "enter dep_request_scope",
                "exit dep_function_scope",
                "background task",
                "exit dep_request_scope",
            ]
        )
    # Same order as for HTTP requests: function-scope dependencies are cleaned
    # up first, then background tasks run, then request-scope dependencies are
    # cleaned up
    assert events == [
        "enter dep_function_scope",
        "enter dep_request_scope",
        "exit dep_function_scope",
        "background task",
        "exit dep_request_scope",
    ]


def test_dependency_overrides() -> None:
    app.dependency_overrides[dep_to_override] = dep_override
    with client.websocket_connect("/override"):
        pass
    assert events == [
        "enter dep_override",
        "endpoint got override",
        "exit dep_override",
    ]


def test_cancellation() -> None:
    async def run_app() -> None:
        incoming = [{"type": "websocket.connect"}]
        task = asyncio.create_task(run_ws_session("/cancel", incoming))
        await asyncio.sleep(0.1)
        task.cancel()
        sent_messages, exception = await task
        # The cancellation is not swallowed nor replaced, it propagates after
        # running the dependency cleanup
        assert isinstance(exception, asyncio.CancelledError)
        # No close message is sent after a cancellation
        assert not any(m["type"] == "websocket.close" for m in sent_messages)

    asyncio.run(run_app())
    # The cancellation runs the dependency cleanup exactly once
    assert events == [
        "enter dep_sync",
        "enter dep_async",
        "exit dep_async",
        "exit dep_sync",
    ]
