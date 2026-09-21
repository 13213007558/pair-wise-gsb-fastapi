"""
Tests for the execution semantics of BackgroundTasks.

These tests pin down the contract for running multiple background tasks
registered on the same response (mixed sync/async, from dependencies and
from the endpoint):

* Tasks run sequentially, in declaration order, and exactly once.
* A failing task does not prevent the other tasks from running, the
  already-started tasks finish (including their finally blocks), and the
  failure is reported (not silently swallowed) after the response was sent.
* Cancellation (client disconnect, app shutdown) propagates immediately and
  is not confused with a task failure.
* The response is already sent when the tasks run, so a failing task can
  never turn the response into an HTTP error.
"""

import asyncio
import contextvars
import threading

import pytest
from fastapi import BackgroundTasks, Depends, FastAPI
from fastapi.background import BackgroundTaskError, BackgroundTaskRecord
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.background import BackgroundTask
from starlette.types import Message, Scope


async def call_asgi(
    app: FastAPI, path: str = "/", messages: list[Message] | None = None
) -> list[Message]:
    """Run the ASGI app directly, collecting the messages sent to the client."""
    if messages is None:
        messages = []
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
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

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    await app(scope, receive, send)
    return messages


def test_tasks_run_in_declaration_order_and_only_once() -> None:
    """Mixed sync/async tasks from the same dependency level and the endpoint
    run in declaration order, each exactly once."""
    app = FastAPI()
    executed: list[str] = []

    def sync_task(name: str) -> None:
        executed.append(name)

    async def async_task(name: str) -> None:
        executed.append(name)

    def dep_one(tasks: BackgroundTasks) -> None:
        tasks.add_task(sync_task, "dep-one-sync")

    def dep_two(tasks: BackgroundTasks) -> None:
        tasks.add_task(async_task, "dep-two-async")

    @app.get("/")
    def endpoint(
        tasks: BackgroundTasks,
        _: None = Depends(dep_one),
        __: None = Depends(dep_two),
    ) -> dict[str, bool]:
        tasks.add_task(sync_task, "endpoint-sync")
        tasks.add_task(async_task, "endpoint-async")
        return {"ok": True}

    client = TestClient(app)
    response = client.get("/")

    assert response.status_code == 200
    assert executed == [
        "dep-one-sync",
        "dep-two-async",
        "endpoint-sync",
        "endpoint-async",
    ]


def test_sync_task_runs_in_threadpool_and_inherits_contextvars() -> None:
    """def tasks run in a worker thread (not the event loop thread) and see
    the contextvars set by the endpoint."""
    app = FastAPI()
    main_thread = threading.get_ident()
    seen: dict[str, object] = {}
    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker")

    def sync_task() -> None:
        seen["thread"] = threading.get_ident()
        seen["marker"] = marker.get("missing")

    @app.get("/")
    async def endpoint(tasks: BackgroundTasks) -> dict[str, bool]:
        marker.set("from-endpoint")
        tasks.add_task(sync_task)
        return {"ok": True}

    client = TestClient(app)
    response = client.get("/")

    assert response.status_code == 200
    assert seen["thread"] != main_thread
    assert seen["marker"] == "from-endpoint"


def test_response_background_is_merged_after_injected_tasks() -> None:
    """A background attached to the returned Response is not dropped and does
    not replace the injected BackgroundTasks: injected tasks (dependencies
    first, then the endpoint) run first, then the response's own."""
    app = FastAPI()
    executed: list[str] = []

    def dep(tasks: BackgroundTasks) -> None:
        tasks.add_task(executed.append, "dependency")

    @app.get("/")
    def endpoint(
        tasks: BackgroundTasks, _: None = Depends(dep)
    ) -> JSONResponse:
        tasks.add_task(executed.append, "endpoint")
        return JSONResponse(
            {"ok": True},
            background=BackgroundTask(executed.append, "response"),
        )

    client = TestClient(app)
    response = client.get("/")

    assert response.status_code == 200
    assert executed == ["dependency", "endpoint", "response"]


@pytest.mark.anyio
async def test_failing_task_finishes_started_tasks_and_reports_error() -> None:
    """When one task fails:

    * the tasks that already started finish (finally blocks run),
    * the remaining tasks still run,
    * the failure reason reaches the caller (monitoring path) as a
      BackgroundTaskError carrying the original exception,
    * the response was already sent and is not rewritten as an HTTP error.
    """
    app = FastAPI()
    events: list[str] = []
    failure = ValueError("task two exploded")

    def first_task() -> None:
        try:
            events.append("first-body")
        finally:
            events.append("first-finally")

    async def failing_task() -> None:
        events.append("second-body")
        raise failure

    def third_task() -> None:
        events.append("third-body")

    @app.get("/")
    def endpoint(tasks: BackgroundTasks) -> dict[str, bool]:
        tasks.add_task(first_task)
        tasks.add_task(failing_task)
        tasks.add_task(third_task)
        return {"ok": True}

    messages: list[Message] = []
    with pytest.raises(BackgroundTaskError) as exc_info:
        await call_asgi(app, messages=messages)

    # The failure is visible to the monitoring path with the original reason.
    error = exc_info.value
    assert error.exceptions == [failure]
    assert "failing_task" in str(error)

    # The response was fully sent before the error and was not rewritten.
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 200
    assert messages[1]["type"] == "http.response.body"
    assert messages[1]["body"] == b'{"ok":true}'

    # Started tasks finished (finally ran), later tasks still ran, once each.
    assert events == ["first-body", "first-finally", "second-body", "third-body"]


@pytest.mark.anyio
async def test_task_records_expose_executed_and_failed_tasks() -> None:
    """The per-task records show which tasks ran, which failed and why."""
    failure = RuntimeError("boom")
    observed: list[BackgroundTaskRecord] = []

    def ok_task() -> None:
        pass

    def bad_task() -> None:
        raise failure

    def on_error(record: BackgroundTaskRecord) -> None:
        # Failure callback, e.g. for monitoring: it gets the failed task and
        # the original exception.
        observed.append(record)

    tasks = BackgroundTasks(on_error=on_error)
    tasks.add_task(ok_task)
    tasks.add_task(bad_task)

    with pytest.raises(BackgroundTaskError):
        await tasks()

    assert [record.status for record in tasks.records] == ["succeeded", "failed"]
    assert tasks.records[0].exception is None
    assert tasks.records[1].exception is failure
    # The failure callback was called with the failed record.
    assert observed == [tasks.records[1]]


@pytest.mark.anyio
async def test_async_on_error_callback_is_awaited() -> None:
    observed: list[str] = []

    def bad_task() -> None:
        raise RuntimeError("boom")

    async def on_error(record: BackgroundTaskRecord) -> None:
        observed.append(record.status)

    tasks = BackgroundTasks(on_error=on_error)
    tasks.add_task(bad_task)

    with pytest.raises(BackgroundTaskError):
        await tasks()

    assert observed == ["failed"]


@pytest.mark.anyio
async def test_tasks_cannot_run_twice() -> None:
    """A second call does not re-run the tasks, it raises instead."""
    counter: list[int] = []

    tasks = BackgroundTasks()
    tasks.add_task(counter.append, 1)

    await tasks()
    assert counter == [1]
    assert tasks.started

    with pytest.raises(RuntimeError, match="only be executed once"):
        await tasks()
    assert counter == [1]

    with pytest.raises(RuntimeError, match="already"):
        tasks.add_task(counter.append, 2)
    assert counter == [1]


@pytest.mark.anyio
async def test_cancellation_is_not_a_task_failure() -> None:
    """Cancellation (client disconnect, app shutdown) propagates immediately,
    the remaining tasks do not run, and it is not reported as a task
    failure."""
    events: list[str] = []

    async def cancelled_task() -> None:
        events.append("cancelled-body")
        raise asyncio.CancelledError()

    def later_task() -> None:
        events.append("later-body")

    tasks = BackgroundTasks()
    tasks.add_task(cancelled_task)
    tasks.add_task(later_task)

    with pytest.raises(asyncio.CancelledError):
        await tasks()

    assert events == ["cancelled-body"]
    assert [record.status for record in tasks.records] == ["cancelled", "pending"]


def test_failing_task_does_not_change_the_sent_response() -> None:
    """From the client's point of view the response is already sent: a
    failing background task cannot turn it into an HTTP error."""
    app = FastAPI()

    def bad_task() -> None:
        raise RuntimeError("boom")

    @app.get("/")
    def endpoint(tasks: BackgroundTasks) -> dict[str, bool]:
        tasks.add_task(bad_task)
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_failing_task_raises_with_test_client() -> None:
    """The failure is not silently printed and treated as success: it
    propagates to the server (here, the test client) with the reason."""
    app = FastAPI()

    def bad_task() -> None:
        raise RuntimeError("boom")

    @app.get("/")
    def endpoint(tasks: BackgroundTasks) -> dict[str, bool]:
        tasks.add_task(bad_task)
        return {"ok": True}

    client = TestClient(app)
    with pytest.raises(BackgroundTaskError, match="bad_task"):
        client.get("/")
