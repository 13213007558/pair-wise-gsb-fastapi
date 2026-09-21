import itertools
import json
from collections.abc import AsyncIterator, Iterator
from typing import Annotated, Any

import httpx
import pytest
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, WebSocket
from fastapi.exceptions import DependencyScopeError
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.types import Message

events: list[str] = []


@pytest.fixture(autouse=True)
def reset_events() -> Iterator[None]:
    events.clear()
    yield


class Resource:
    def __init__(self, name: str) -> None:
        self.name = name
        self.open = True
        events.append(f"enter:{name}")

    def close(self) -> None:
        self.open = False
        events.append(f"exit:{self.name}")


uncached_counter = itertools.count(1)


def dep_uncached() -> Iterator[Resource]:
    resource = Resource(f"uncached-{next(uncached_counter)}")
    yield resource
    resource.close()


def dep_cached() -> Iterator[Resource]:
    resource = Resource("cached")
    yield resource
    resource.close()


def dep_function() -> Iterator[Resource]:
    resource = Resource("function")
    yield resource
    resource.close()


def dep_request() -> Iterator[Resource]:
    resource = Resource("request")
    yield resource
    resource.close()


async def dep_async_uncached() -> AsyncIterator[Resource]:
    resource = Resource(f"async-uncached-{next(uncached_counter)}")
    yield resource
    resource.close()


UncachedDep = Annotated[Resource, Depends(dep_uncached, scope="uncached")]
AsyncUncachedDep = Annotated[Resource, Depends(dep_async_uncached, scope="uncached")]
CachedDep = Annotated[Resource, Depends(dep_cached)]
FunctionDep = Annotated[Resource, Depends(dep_function, scope="function")]
RequestDep = Annotated[Resource, Depends(dep_request, scope="request")]


def dep_child_cached() -> Resource:
    return Resource("child-cached")


ChildCachedDep = Annotated[Resource, Depends(dep_child_cached)]


def dep_parent_uncached(child: ChildCachedDep) -> tuple[Resource, Resource]:
    return Resource(f"parent-{next(uncached_counter)}"), child


ParentUncachedDep = Annotated[
    tuple[Resource, Resource], Depends(dep_parent_uncached, scope="uncached")
]


class Service:
    def __init__(self) -> None:
        self.id = next(uncached_counter)


ServiceDep = Annotated[Service, Depends(Service, scope="uncached")]


class CallableService:
    def __call__(self) -> int:
        return next(uncached_counter)


callable_service = CallableService()
CallableServiceDep = Annotated[int, Depends(callable_service, scope="uncached")]


class Item(BaseModel):
    name: str


app = FastAPI()


@app.get("/cache-identity")
def cache_identity(a: UncachedDep, b: UncachedDep, c: CachedDep, d: CachedDep) -> Any:
    return {
        "uncached_distinct": a is not b,
        "uncached_names": [a.name, b.name],
        "cached_shared": c is d,
    }


@app.get("/class-identity")
def class_identity(a: ServiceDep, b: ServiceDep) -> Any:
    return {"class_distinct": a is not b, "class_ids": [a.id, b.id]}


@app.get("/callable-instance")
def callable_instance(x: CallableServiceDep, y: CallableServiceDep) -> Any:
    return {"values": [x, y], "distinct": x != y}


@app.get("/nested")
def nested(a: ParentUncachedDep, b: ParentUncachedDep) -> Any:
    return {
        "parents_distinct": a[0] is not b[0],
        "child_shared": a[1] is b[1],
    }


@app.get("/streaming")
def streaming(
    func_dep: FunctionDep, req_dep: RequestDep, unc_dep: UncachedDep
) -> Any:
    events.append("endpoint:streaming")

    def iter_data() -> Iterator[str]:
        events.append(
            f"stream:func={func_dep.open},req={req_dep.open},unc={unc_dep.open}"
        )
        yield json.dumps(
            {
                "func_open": func_dep.open,
                "req_open": req_dep.open,
                "unc_open": unc_dep.open,
            }
        )

    return StreamingResponse(iter_data())


@app.get("/async-generator")
def async_generator(a: AsyncUncachedDep, b: AsyncUncachedDep) -> Any:
    events.append("endpoint:async-generator")
    return {"distinct": a is not b, "names": [a.name, b.name]}


@app.get("/background")
def background(background_tasks: BackgroundTasks, unc: UncachedDep) -> Any:
    def task() -> None:
        events.append(f"background:open={unc.open}")

    background_tasks.add_task(task)
    return {"status": "ok"}


@app.get("/validated", response_model=Item)
def validated(unc: UncachedDep) -> Any:
    events.append("endpoint:validated")
    return {"name": unc.name, "secret": "filtered-out"}


def dep_uncached_raises() -> Iterator[Resource]:
    resource = Resource("uncached-raises")
    try:
        yield resource
    except HTTPException as exc:
        events.append(f"exit-with-exc:{exc.status_code}")
        raise
    finally:
        resource.close()


@app.get("/exception")
def exception_endpoint(
    unc: Annotated[Resource, Depends(dep_uncached_raises, scope="uncached")],
) -> Any:
    events.append("endpoint:exception")
    raise HTTPException(status_code=418, detail="teapot")


@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket, a: AsyncUncachedDep, b: AsyncUncachedDep
) -> None:
    await websocket.accept()
    events.append("endpoint:ws")
    await websocket.send_json(
        {"distinct": a is not b, "open": a.open and b.open}
    )
    await websocket.close()


client = TestClient(app)


def test_uncached_not_cached() -> None:
    response = client.get("/cache-identity")
    assert response.status_code == 200
    data = response.json()
    assert data["uncached_distinct"] is True
    assert data["uncached_names"][0] != data["uncached_names"][1]
    # The default behavior of executing a repeated dependency only once is kept
    assert data["cached_shared"] is True
    assert events.count("enter:cached") == 1
    uncached_exits = [event for event in events if event.startswith("exit:uncached-")]
    assert len(uncached_exits) == 2


def test_uncached_class_dependency() -> None:
    response = client.get("/class-identity")
    assert response.status_code == 200
    data = response.json()
    assert data["class_distinct"] is True
    assert data["class_ids"][0] != data["class_ids"][1]


def test_uncached_callable_instance() -> None:
    response = client.get("/callable-instance")
    assert response.status_code == 200
    data = response.json()
    assert data["distinct"] is True


def test_uncached_parent_with_cached_child() -> None:
    response = client.get("/nested")
    assert response.status_code == 200
    data = response.json()
    # The uncached parent runs once per declaration...
    assert data["parents_distinct"] is True
    # ...but its cached sub-dependency is still shared between both runs
    assert data["child_shared"] is True
    assert events.count("enter:child-cached") == 1


def test_streaming_exit_timeline() -> None:
    response = client.get("/streaming")
    assert response.status_code == 200
    data = response.json()
    # The function scoped dependency is closed before the response is streamed
    assert data["func_open"] is False
    # Request and uncached dependencies are still open while streaming
    assert data["req_open"] is True
    assert data["unc_open"] is True
    stream_event = next(event for event in events if event.startswith("stream:"))
    stream_index = events.index(stream_event)
    assert events.index("exit:function") < stream_index
    assert events.index("exit:request") > stream_index
    uncached_exit = next(event for event in events if event.startswith("exit:uncached-"))
    assert events.index(uncached_exit) > stream_index
    # Everything is closed once the request is done
    assert "exit:request" in events
    assert "exit:function" in events


def test_async_generator_uncached() -> None:
    response = client.get("/async-generator")
    assert response.status_code == 200
    data = response.json()
    assert data["distinct"] is True
    endpoint_index = events.index("endpoint:async-generator")
    async_exits = [
        event for event in events if event.startswith("exit:async-uncached-")
    ]
    assert len(async_exits) == 2
    for exit_event in async_exits:
        assert events.index(exit_event) > endpoint_index


def test_background_tasks_run_before_uncached_exit() -> None:
    response = client.get("/background")
    assert response.status_code == 200
    assert "background:open=True" in events
    background_index = events.index("background:open=True")
    uncached_exit = next(event for event in events if event.startswith("exit:uncached-"))
    assert events.index(uncached_exit) > background_index


def test_response_validation_with_uncached() -> None:
    response = client.get("/validated")
    assert response.status_code == 200
    # The response model filters the extra field, the dependency stays open
    # until after the (validated) response is sent
    data = response.json()
    assert data["name"].startswith("uncached-")
    assert "secret" not in data
    endpoint_index = events.index("endpoint:validated")
    uncached_exit = next(event for event in events if event.startswith("exit:uncached-"))
    assert events.index(uncached_exit) > endpoint_index


def test_exception_chain_reaches_uncached_dependency() -> None:
    response = client.get("/exception")
    assert response.status_code == 418
    assert response.json() == {"detail": "teapot"}
    # The original exception is propagated to the dependency after yield
    assert "exit-with-exc:418" in events
    assert events.index("exit-with-exc:418") > events.index("endpoint:exception")
    assert "exit:uncached-raises" in events


def test_websocket_uncached() -> None:
    with client.websocket_connect("/ws") as websocket:
        data = websocket.receive_json()
    assert data["distinct"] is True
    assert data["open"] is True
    # Both async generator dependencies are closed after the WebSocket handler
    async_exits = [
        event for event in events if event.startswith("exit:async-uncached-")
    ]
    assert len(async_exits) == 2
    for exit_event in async_exits:
        assert events.index(exit_event) > events.index("endpoint:ws")


def test_dependency_overrides_keep_uncached_scope() -> None:
    def override_dep() -> Iterator[Resource]:
        resource = Resource("override")
        yield resource
        resource.close()

    app.dependency_overrides[dep_uncached] = override_dep
    try:
        response = client.get("/cache-identity")
        assert response.status_code == 200
        data = response.json()
        # The override is used and still not cached: it runs once per declaration
        assert data["uncached_distinct"] is True
        assert events.count("enter:override") == 2
        assert events.count("exit:override") == 2
        assert data["cached_shared"] is True
    finally:
        app.dependency_overrides = {}


def test_uncached_cannot_depend_on_function_scope() -> None:
    def broken_parent(child: FunctionDep) -> Iterator[None]:
        yield  # pragma: no cover

    with pytest.raises(
        DependencyScopeError,
        match='The dependency "broken_parent" has a scope of '
        '"uncached", it cannot depend on dependencies with scope "function"',
    ):

        @app.get("/broken-uncached-parent")
        def broken_endpoint(
            dep: Annotated[None, Depends(broken_parent, scope="uncached")],
        ) -> None:  # pragma: no cover
            pass


def test_function_scope_can_depend_on_uncached() -> None:
    def function_parent(child: UncachedDep) -> Iterator[str]:
        yield f"child-open={child.open}"

    local_app = FastAPI()

    @local_app.get("/function-parent")
    def function_parent_endpoint(
        value: Annotated[str, Depends(function_parent, scope="function")],
    ) -> Any:
        return {"value": value}

    local_client = TestClient(local_app)
    response = local_client.get("/function-parent")
    assert response.status_code == 200
    assert response.json() == {"value": "child-open=True"}


@pytest.mark.anyio
async def test_async_client_streaming() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as async_client:
        response = await async_client.get("/streaming")
    assert response.status_code == 200
    data = response.json()
    assert data["func_open"] is False
    assert data["req_open"] is True
    assert data["unc_open"] is True
    stream_event = next(event for event in events if event.startswith("stream:"))
    stream_index = events.index(stream_event)
    assert events.index("exit:function") < stream_index
    assert events.index("exit:request") > stream_index
    uncached_exit = next(event for event in events if event.startswith("exit:uncached-"))
    assert events.index(uncached_exit) > stream_index


@pytest.mark.anyio
async def test_real_asgi_call() -> None:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/cache-identity",
        "raw_path": b"/cache-identity",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    await app(scope, receive, send)
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 200
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    data = json.loads(body)
    assert data["uncached_distinct"] is True
    assert data["cached_shared"] is True
    # All uncached resources are closed by the time the ASGI call returns
    uncached_exits = [event for event in events if event.startswith("exit:uncached-")]
    assert len(uncached_exits) == 2
