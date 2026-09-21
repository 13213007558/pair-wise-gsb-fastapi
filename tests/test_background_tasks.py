import asyncio
import time
from contextvars import ContextVar
from threading import Event, get_ident
from typing import Annotated

import pytest
from fastapi import BackgroundTasks, BackgroundTasksError, Depends, FastAPI
from fastapi.exceptions import FastAPIError
from fastapi.testclient import TestClient
from fastapi.websockets import WebSocket
from starlette.background import BackgroundTasks as StarletteBackgroundTasks
from starlette.responses import Response


def test_background_tasks_preserve_order_continue_after_failure_and_finalize():
    app = FastAPI()
    events = []
    failures = []

    def sync_start():
        events.append("sync-start")

    async def async_fail():
        try:
            events.append("async-fail-start")
            raise RuntimeError("async failure")
        finally:
            events.append("async-fail-finally")

    def sync_after():
        events.append("sync-after")

    def on_failure(task, error):
        failures.append((task.func.__name__, str(error)))

    tasks = BackgroundTasks(on_error=on_failure)
    tasks.add_task(sync_start)
    tasks.add_task(async_fail)
    tasks.add_task(sync_after)

    @app.get("/")
    def endpoint():
        return Response("ok", background=tasks)

    client = TestClient(app)
    with pytest.raises(RuntimeError, match="async failure"):
        client.get("/")

    assert events == [
        "sync-start",
        "async-fail-start",
        "async-fail-finally",
        "sync-after",
    ]
    assert failures == [("async_fail", "async failure")]


def test_dependency_and_response_background_tasks_merge_and_run_once():
    app = FastAPI()
    events = []

    def dependency(background_tasks: BackgroundTasks):
        background_tasks.add_task(events.append, "dependency")

    def response_task():
        events.append("response")

    @app.get("/")
    def endpoint(
        background_tasks: BackgroundTasks,
        _: Annotated[None, Depends(dependency)],
    ):
        background_tasks.add_task(events.append, "endpoint")
        explicit_tasks = StarletteBackgroundTasks()
        explicit_tasks.add_task(response_task)
        return Response("ok", background=explicit_tasks)

    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert response.text == "ok"
    assert events == ["dependency", "endpoint", "response"]


def test_background_tasks_at_same_dependency_level_preserve_declaration_order():
    app = FastAPI()
    events = []

    def first_dependency(background_tasks: BackgroundTasks):
        background_tasks.add_task(events.append, "first")

    def second_dependency(background_tasks: BackgroundTasks):
        background_tasks.add_task(events.append, "second")

    @app.get("/")
    def endpoint(
        background_tasks: BackgroundTasks,
        first: Annotated[None, Depends(first_dependency)],
        second: Annotated[None, Depends(second_dependency)],
    ):
        background_tasks.add_task(events.append, "endpoint")

    TestClient(app).get("/")

    assert events == ["first", "second", "endpoint"]


def test_injected_background_tasks_used_as_response_background_run_once():
    app = FastAPI()
    events = []

    def dependency(background_tasks: BackgroundTasks):
        background_tasks.add_task(events.append, "dependency")

    @app.get("/")
    def endpoint(
        background_tasks: BackgroundTasks,
        _: Annotated[None, Depends(dependency)],
    ):
        background_tasks.add_task(events.append, "endpoint")
        return Response("ok", background=background_tasks)

    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert events == ["dependency", "endpoint"]


def test_background_tasks_instance_is_idempotent():
    events = []
    tasks = BackgroundTasks()
    tasks.add_task(events.append, "once")

    asyncio.run(tasks())
    asyncio.run(tasks())

    assert events == ["once"]


def test_sync_background_task_runs_in_thread_with_contextvars():
    app = FastAPI()
    value: ContextVar[str] = ContextVar("background-value")
    thread_ids = set()

    async def dependency():
        value.set("dependency-value")

    def sync_task():
        thread_ids.add(get_ident())

    @app.get("/")
    async def endpoint(
        background_tasks: BackgroundTasks,
        _: Annotated[None, Depends(dependency)],
    ):
        background_tasks.add_task(sync_task)
        return {"value": value.get()}

    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert response.json() == {"value": "dependency-value"}
    assert thread_ids
    assert thread_ids != {get_ident()}


def test_background_failure_is_visible_after_response_is_sent():
    app = FastAPI()
    failures = []

    def failing_task():
        raise RuntimeError("after response")

    @app.get("/")
    def endpoint(background_tasks: BackgroundTasks):
        background_tasks.on_error = lambda task, error: failures.append(str(error))
        background_tasks.add_task(failing_task)
        return {"status": "ok"}

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert failures == ["after response"]


def test_multiple_background_failures_are_aggregated_and_tasks_continue():
    events = []
    failures = []
    tasks = BackgroundTasks(on_error=lambda task, error: failures.append(str(error)))

    async def first_failure():
        events.append("first")
        raise RuntimeError("first failure")

    def middle():
        events.append("middle")

    async def second_failure():
        events.append("second")
        raise ValueError("second failure")

    tasks.add_task(first_failure)
    tasks.add_task(middle)
    tasks.add_task(second_failure)

    with pytest.raises(BackgroundTasksError) as exc_info:
        asyncio.run(tasks())

    assert events == ["first", "middle", "second"]
    assert failures == ["first failure", "second failure"]
    assert [type(error).__name__ for error in exc_info.value.errors] == [
        "RuntimeError",
        "ValueError",
    ]


def test_failing_error_callback_keeps_original_task_failure():
    def failing_task():
        raise RuntimeError("task failure")

    def failing_callback(task, error):
        raise RuntimeError("callback failure")

    tasks = BackgroundTasks(on_error=failing_callback)
    tasks.add_task(failing_task)

    with pytest.raises(BackgroundTasksError) as exc_info:
        asyncio.run(tasks())

    assert [str(error) for error in exc_info.value.errors] == [
        "task failure",
        "callback failure",
    ]


def test_custom_background_tasks_subclass_is_not_flattened_during_merge():
    events = []
    failures = []

    class CustomTasks(BackgroundTasks):
        async def __call__(self):
            events.append("custom-call")
            await super().__call__()

    def failing_task():
        raise RuntimeError("custom failure")

    custom_tasks = CustomTasks(on_error=lambda task, error: failures.append(str(error)))
    custom_tasks.add_task(failing_task)
    app = FastAPI()

    @app.get("/")
    async def endpoint():
        return Response("ok", background=custom_tasks)

    with pytest.raises(RuntimeError, match="custom failure"):
        TestClient(app).get("/")

    assert events == ["custom-call"]
    assert failures == ["custom failure"]


@pytest.mark.anyio
async def test_client_disconnect_after_response_does_not_cancel_background_tasks():
    app = FastAPI()
    events = []
    background_started = asyncio.Event()
    disconnect_seen = False
    sent_messages = []

    async def wait_for_disconnect():
        nonlocal disconnect_seen
        disconnect_seen = (await receive())["type"] == "http.disconnect"

    async def background_task():
        events.append("started")
        background_started.set()
        await wait_for_disconnect()
        await asyncio.sleep(0)
        events.append("finished")

    @app.get("/")
    async def endpoint(background_tasks: BackgroundTasks):
        background_tasks.add_task(background_task)
        return {"status": "ok"}

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent_messages.append(message)

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/",
        "raw_path": b"/",
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "state": {},
    }

    await asyncio.wait_for(app(scope, receive, send), timeout=1)

    assert sent_messages[0]["type"] == "http.response.start"
    assert sent_messages[0]["status"] == 200
    assert disconnect_seen is True
    assert events == ["started", "finished"]


@pytest.mark.anyio
async def test_shutdown_cancellation_runs_started_task_finally_only():
    app = FastAPI()
    events = []
    response_sent = asyncio.Event()
    background_started = asyncio.Event()

    async def cancellable_background_task():
        try:
            events.append("started")
            background_started.set()
            await asyncio.Event().wait()
        finally:
            events.append("finally")

    async def later_background_task():
        events.append("later")

    @app.get("/")
    async def endpoint(background_tasks: BackgroundTasks):
        background_tasks.add_task(cancellable_background_task)
        background_tasks.add_task(later_background_task)
        return {"status": "ok"}

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        if message["type"] == "http.response.body" and not message.get("more_body"):
            response_sent.set()

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/",
        "raw_path": b"/",
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "state": {},
    }

    request_task = asyncio.create_task(app(scope, receive, send))
    await response_sent.wait()
    await background_started.wait()
    request_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await request_task

    assert events == ["started", "finally"]


def test_background_task_cancellation_stops_remaining_tasks_and_runs_finally():
    events = []

    async def cancelled_task():
        try:
            events.append("started")
            await asyncio.sleep(10)
        finally:
            events.append("finally")

    async def later_task():
        events.append("later")

    tasks = BackgroundTasks()
    tasks.add_task(cancelled_task)
    tasks.add_task(later_task)

    async def run():
        task = asyncio.create_task(tasks())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert events == ["started", "finally"]


@pytest.mark.anyio
async def test_sync_background_task_cancellation_waits_for_thread_completion():
    events = []
    sync_started = Event()
    sync_finished = Event()

    def sync_task():
        try:
            events.append("sync-started")
            sync_started.set()
            time.sleep(0.05)
        finally:
            events.append("sync-finally")
            sync_finished.set()

    async def later_task():
        events.append("later")

    tasks = BackgroundTasks()
    tasks.add_task(sync_task)
    tasks.add_task(later_task)

    request_task = asyncio.create_task(tasks())
    await asyncio.to_thread(sync_started.wait, 1)
    request_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await request_task

    await asyncio.to_thread(sync_finished.wait, 1)

    assert events == ["sync-started", "sync-finally"]


def test_background_tasks_cannot_be_used_with_websockets():
    app = FastAPI()

    with pytest.raises(FastAPIError, match="HTTP path operations"):

        @app.websocket("/ws")
        async def websocket_endpoint(
            websocket: WebSocket,
            background_tasks: BackgroundTasks,
        ):
            await websocket.accept()
