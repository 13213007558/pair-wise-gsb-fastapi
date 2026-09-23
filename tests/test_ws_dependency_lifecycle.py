import asyncio

import anyio
import pytest
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Security,
    WebSocket,
    WebSocketDisconnect,
    WebSocketException,
    status,
)
from fastapi.middleware import Middleware
from fastapi.testclient import TestClient
from pydantic import BaseModel, StrictInt, ValidationError
from starlette.types import ASGIApp, Message, Receive, Scope, Send


def make_app(*, middleware: list | None = None) -> tuple[FastAPI, list[str]]:
    events: list[str] = []
    app = FastAPI(middleware=middleware or [])

    def sync_dep() -> str:
        events.append("sync-enter")
        return "sync"

    async def async_dep() -> str:
        events.append("async-enter")
        return "async"

    def sync_yield_dep():
        events.append("sync-yield-enter")
        yield "sync-yield"
        events.append("sync-yield-exit")

    async def async_yield_dep():
        events.append("async-yield-enter")
        yield "async-yield"
        events.append("async-yield-exit")

    async def cleanup_error_dep():
        yield
        events.append("cleanup-error")
        raise WebSocketException(code=1011, reason="cleanup")

    def cleanup_value_error_dep():
        yield
        events.append("cleanup-value-error")
        raise ValueError("cleanup failure")

    def cleanup_validation_error_dep():
        yield
        events.append("cleanup-validation-error")
        StrictValue(value="invalid")

    async def cancel_dep():
        events.append("cancel-yield-enter")
        try:
            yield
        finally:
            events.append("cancel-yield-exit")

    async def api_key() -> str:
        events.append("security-enter")
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="unauthorized",
        )

    @app.websocket("/pre-http")
    async def pre_http(websocket: WebSocket, _: str = Depends(sync_dep)):
        raise HTTPException(status_code=403, detail="forbidden")

    @app.websocket("/pre-ws")
    async def pre_ws(websocket: WebSocket, _: str = Depends(async_dep)):
        raise WebSocketException(code=1008, reason="denied")

    async def successful_yield_before_rejection():
        events.append("partial-yield-enter")
        try:
            yield
        finally:
            events.append("partial-yield-exit")

    async def rejecting_dependency() -> None:
        events.append("rejecting-enter")
        raise WebSocketException(code=1008, reason="partial")

    @app.websocket("/pre-partial-rejection")
    async def pre_partial_rejection(
        websocket: WebSocket,
        opened: None = Depends(successful_yield_before_rejection),
        dependency: None = Depends(rejecting_dependency),
    ):
        raise WebSocketException(code=1008, reason="denied")

    @app.websocket("/pre-validation")
    async def pre_validation(websocket: WebSocket, x: int | None = None):
        await websocket.accept()

    class StrictValue(BaseModel):
        value: StrictInt

    @app.websocket("/pre-pydantic-validation")
    async def pre_pydantic_validation(websocket: WebSocket, value: str):
        StrictValue(value=value)
        await websocket.accept()

    @app.websocket("/post-http")
    async def post_http(
        websocket: WebSocket,
        sync_value: str = Depends(sync_dep),
        async_value: str = Depends(async_dep),
    ):
        await websocket.accept()
        raise HTTPException(status_code=403, detail="forbidden")

    @app.websocket("/disconnect")
    async def disconnect(
        websocket: WebSocket,
        request_value=Depends(async_yield_dep),
        function_value=Depends(sync_yield_dep, scope="function"),
    ):
        await websocket.accept()
        try:
            await websocket.receive_text()
        except WebSocketDisconnect as exc:
            events.append(f"disconnect:{exc.code}:{exc.reason}")
            raise

    @app.websocket("/swallowed-disconnect")
    async def swallowed_disconnect(
        websocket: WebSocket,
        request_value=Depends(async_yield_dep),
        function_value=Depends(sync_yield_dep, scope="function"),
    ):
        await websocket.accept()
        try:
            await websocket.receive_text()
        except WebSocketDisconnect as exc:
            events.append(f"swallowed:{exc.code}:{exc.reason}")

    @app.websocket("/active-close-cleanup-error")
    async def active_close_cleanup_error(
        websocket: WebSocket,
        _: None = Depends(cleanup_error_dep),
    ):
        await websocket.accept()
        await websocket.close(code=1000, reason="done")

    @app.websocket("/active-close-generic-cleanup-error")
    async def active_close_generic_cleanup_error(
        websocket: WebSocket,
        _: None = Depends(cleanup_value_error_dep),
    ):
        await websocket.accept()
        await websocket.close(code=1000, reason="done")

    @app.websocket("/cleanup-validation-error")
    async def cleanup_validation_error(
        websocket: WebSocket,
        _: None = Depends(cleanup_validation_error_dep),
    ):
        await websocket.accept()

    @app.websocket("/cancel")
    async def cancel(websocket: WebSocket, _: None = Depends(cancel_dep)):
        await websocket.accept()
        await anyio.sleep_forever()

    @app.websocket("/background")
    async def background(
        websocket: WebSocket,
        tasks: BackgroundTasks,
        function_value=Depends(sync_yield_dep, scope="function"),
        request_value=Depends(async_yield_dep),
    ):
        def task() -> None:
            events.append("background")

        tasks.add_task(task)
        await websocket.accept()
        await websocket.close()

    @app.websocket("/security")
    async def secure(
        websocket: WebSocket,
        token: str = Security(api_key, scopes=["ws"]),
    ):
        await websocket.accept()
        await websocket.close()

    app.dependency_overrides[sync_yield_dep] = sync_yield_dep
    app.dependency_overrides[api_key] = lambda: "override-token"
    return app, events


def test_pre_accept_http_exception_denies_handshake() -> None:
    app, _ = make_app()
    client = TestClient(app)

    with pytest.raises(Exception) as exc_info:
        with client.websocket_connect("/pre-http"):
            pass

    assert exc_info.value.status_code == 403


def test_pre_accept_websocket_exception_closes_handshake() -> None:
    app, _ = make_app()
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/pre-ws"):
            pass

    assert exc_info.value.code == 1008
    assert exc_info.value.reason == "denied"


def test_pre_accept_rejection_closes_only_entered_dependencies() -> None:
    app, events = make_app()
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/pre-partial-rejection"):
            pass

    assert exc_info.value.reason == "partial"
    assert events.count("partial-yield-enter") == 1
    assert events.count("partial-yield-exit") == 1
    assert events.count("rejecting-enter") == 1


def test_pre_accept_validation_error_reason_is_string() -> None:
    app, _ = make_app()
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/pre-validation?x=not-an-int"):
            pass

    assert exc_info.value.code == 1008
    assert isinstance(exc_info.value.reason, str)
    assert '"type":"int_parsing"' in exc_info.value.reason


def test_pre_accept_pydantic_validation_error_maps_to_close() -> None:
    app, _ = make_app()
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/pre-pydantic-validation?value=not-an-int"):
            pass

    assert exc_info.value.code == 1008
    assert isinstance(exc_info.value.reason, str)
    assert '"type":"int_type"' in exc_info.value.reason


def test_validation_reason_respects_websocket_reason_limit() -> None:
    app, _ = make_app()
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/pre-validation?x=" + "x" * 200
        ):
            pass

    assert exc_info.value.code == 1008
    assert len(exc_info.value.reason.encode("utf-8")) <= 123


def test_post_accept_http_exception_maps_to_websocket_close() -> None:
    app, events = make_app()
    client = TestClient(app)

    with client.websocket_connect("/post-http") as websocket:
        message = websocket.receive()

    assert message["code"] == 1008
    assert message["reason"] == "forbidden"
    assert "sync-enter" in events
    assert "async-enter" in events


def test_disconnect_runs_cleanup_without_second_close() -> None:
    app, events = make_app()
    sent_messages: list[dict] = []

    async def receive() -> dict:
        if not getattr(receive, "connected_sent", False):
            receive.connected_sent = True
            return {"type": "websocket.connect"}
        return {"type": "websocket.disconnect", "code": 1001, "reason": "gone"}

    async def send(message: dict) -> None:
        sent_messages.append(message)

    scope = {
        "type": "websocket",
        "path": "/disconnect",
        "raw_path": b"/disconnect",
        "root_path": "",
        "headers": [],
        "query_string": b"",
        "client": ("testclient", 123),
        "server": ("testserver", 80),
        "scheme": "ws",
        "subprotocols": [],
        "state": {},
        "extensions": {"websocket.http.response": {}},
    }

    with pytest.raises(WebSocketDisconnect) as exc_info:
        asyncio.run(app(scope, receive, send))

    assert exc_info.value.code == 1001
    assert exc_info.value.reason == "gone"
    assert [message["type"] for message in sent_messages] == ["websocket.accept"]
    assert events == [
        "async-yield-enter",
        "sync-yield-enter",
        "disconnect:1001:gone",
        "sync-yield-exit",
        "async-yield-exit",
    ]


def test_swallowed_disconnect_does_not_send_close_after_cleanup() -> None:
    app, events = make_app()
    sent_messages: list[dict] = []

    async def receive() -> dict:
        if not getattr(receive, "connected_sent", False):
            receive.connected_sent = True
            return {"type": "websocket.connect"}
        return {"type": "websocket.disconnect", "code": 1001, "reason": "gone"}

    async def send(message: dict) -> None:
        sent_messages.append(message)

    scope = {
        "type": "websocket",
        "path": "/swallowed-disconnect",
        "raw_path": b"/swallowed-disconnect",
        "root_path": "",
        "headers": [],
        "query_string": b"",
        "client": ("testclient", 123),
        "server": ("testserver", 80),
        "scheme": "ws",
        "subprotocols": [],
        "state": {},
        "extensions": {"websocket.http.response": {}},
    }

    asyncio.run(app(scope, receive, send))

    assert [message["type"] for message in sent_messages] == ["websocket.accept"]
    assert events == [
        "async-yield-enter",
        "sync-yield-enter",
        "swallowed:1001:gone",
        "sync-yield-exit",
        "async-yield-exit",
    ]


def test_active_close_preserves_code_and_does_not_close_twice() -> None:
    app, events = make_app()
    client = TestClient(app, raise_server_exceptions=False)

    with pytest.raises(WebSocketException) as exc_info:
        with client.websocket_connect(
            "/active-close-cleanup-error"
        ) as websocket:
            message = websocket.receive()
            assert message == {
                "type": "websocket.close",
                "code": 1000,
                "reason": "done",
            }

    assert exc_info.value.code == 1011
    assert exc_info.value.reason == "cleanup"
    assert events == ["cleanup-error"]


def test_active_close_generic_cleanup_error_preserves_original_close() -> None:
    app, events = make_app()
    client = TestClient(app, raise_server_exceptions=False)

    with pytest.raises(ValueError, match="cleanup failure"):
        with client.websocket_connect(
            "/active-close-generic-cleanup-error"
        ) as websocket:
            message = websocket.receive()

    assert message == {
        "type": "websocket.close",
        "code": 1000,
        "reason": "done",
    }
    assert events == ["cleanup-value-error"]


def test_cleanup_validation_error_maps_to_close() -> None:
    app, events = make_app()
    client = TestClient(app)

    with client.websocket_connect("/cleanup-validation-error") as websocket:
        message = websocket.receive()

    assert message["code"] == status.WS_1008_POLICY_VIOLATION
    assert isinstance(message["reason"], str)
    assert "int_type" in message["reason"]
    assert events == ["cleanup-validation-error"]


def test_background_tasks_send_only_one_close() -> None:
    close_messages: list[dict] = []

    class CountingCloseMiddleware:
        def __init__(self, inner_app: ASGIApp) -> None:
            self.inner_app: ASGIApp = inner_app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            async def counting_send(message: Message) -> None:
                if (
                    scope["type"] == "websocket"
                    and message["type"] == "websocket.close"
                ):
                    close_messages.append(message)
                await send(message)

            await self.inner_app(scope, receive, counting_send)

    app, _ = make_app(middleware=[Middleware(CountingCloseMiddleware)])
    client = TestClient(app)

    with client.websocket_connect("/background") as websocket:
        websocket.receive()

    assert close_messages == [{"type": "websocket.close", "code": 1000, "reason": ""}]


def test_cancellation_runs_cleanup_without_close() -> None:
    app, events = make_app()
    sent_messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "websocket.connect"}

    async def send(message: dict) -> None:
        sent_messages.append(message)

    scope = {
        "type": "websocket",
        "path": "/cancel",
        "raw_path": b"/cancel",
        "root_path": "",
        "headers": [],
        "query_string": b"",
        "client": ("testclient", 123),
        "server": ("testserver", 80),
        "scheme": "ws",
        "subprotocols": [],
        "state": {},
        "extensions": {"websocket.http.response": {}},
    }

    async def run_canceled_request() -> None:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(app, scope, receive, send)
            await anyio.sleep(0)
            task_group.cancel_scope.cancel()

    asyncio.run(run_canceled_request())
    assert [message["type"] for message in sent_messages] == ["websocket.accept"]
    assert events == ["cancel-yield-enter", "cancel-yield-exit"]


def test_background_tasks_run_between_dependency_boundaries() -> None:
    app, events = make_app()
    client = TestClient(app)

    with client.websocket_connect("/background") as websocket:
        websocket.receive()

    assert events == [
        "sync-yield-enter",
        "async-yield-enter",
        "sync-yield-exit",
        "background",
        "async-yield-exit",
    ]


def test_security_dependency_uses_overrides() -> None:
    app, _ = make_app()
    client = TestClient(app)

    with client.websocket_connect("/security"):
        pass


def test_security_dependency_denial_reason() -> None:
    app, events = make_app()
    app.dependency_overrides.clear()
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/security"):
            pass

    assert exc_info.value.code == status.WS_1008_POLICY_VIOLATION
    assert exc_info.value.reason == "unauthorized"
    assert events == ["security-enter"]
