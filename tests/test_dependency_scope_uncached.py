import json
from typing import Annotated, Any

import pytest
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, WebSocket
from fastapi.exceptions import DependencyScopeError
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

events: list[str] = []
counter: list[int] = [0]


@pytest.fixture(autouse=True)
def reset_state() -> None:
    events.clear()
    counter[0] = 0


def next_value() -> int:
    counter[0] += 1
    return counter[0]


def counter_dep() -> int:
    return next_value()


CachedCounterDep = Annotated[int, Depends(counter_dep)]
NoCacheCounterDep = Annotated[int, Depends(counter_dep, use_cache=False)]
UncachedCounterDep = Annotated[int, Depends(counter_dep, scope="uncached")]


class Resource:
    def __init__(self, name: str) -> None:
        self.name = name
        self.open = True


def request_resource() -> Any:
    resource = Resource("request")
    events.append("enter-request")
    yield resource
    resource.open = False
    events.append("exit-request")


def function_resource() -> Any:
    resource = Resource("function")
    events.append("enter-function")
    yield resource
    resource.open = False
    events.append("exit-function")


def uncached_resource() -> Any:
    resource = Resource("uncached")
    events.append("enter-uncached")
    try:
        yield resource
    finally:
        resource.open = False
        events.append("exit-uncached")


async def async_uncached_resource() -> Any:
    resource = Resource("async-uncached")
    events.append("enter-async-uncached")
    try:
        yield resource
    finally:
        resource.open = False
        events.append("exit-async-uncached")


RequestResourceDep = Annotated[Resource, Depends(request_resource, scope="request")]
FunctionResourceDep = Annotated[Resource, Depends(function_resource, scope="function")]
UncachedResourceDep = Annotated[Resource, Depends(uncached_resource, scope="uncached")]
AsyncUncachedResourceDep = Annotated[
    Resource, Depends(async_uncached_resource, scope="uncached")
]


class ClassDependency:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        return self.calls


class_dependency = ClassDependency()
UncachedClassDep = Annotated[int, Depends(class_dependency, scope="uncached")]


def sub_cached(value: CachedCounterDep) -> int:
    return value


def sub_uncached(value: UncachedCounterDep) -> int:
    return value


SubCachedDep = Annotated[int, Depends(sub_cached)]
SubUncachedDep = Annotated[int, Depends(sub_uncached, scope="uncached")]


app = FastAPI()


@app.get("/cache-identity")
def get_cache_identity(
    cached_a: CachedCounterDep,
    cached_b: CachedCounterDep,
    uncached_a: UncachedCounterDep,
    uncached_b: UncachedCounterDep,
) -> Any:
    return {
        "cached_a": cached_a,
        "cached_b": cached_b,
        "uncached_a": uncached_a,
        "uncached_b": uncached_b,
    }


@app.get("/sub-cache-identity")
def get_sub_cache_identity(
    sub_cached_a: SubCachedDep,
    sub_cached_b: SubCachedDep,
    sub_uncached_a: SubUncachedDep,
    sub_uncached_b: SubUncachedDep,
) -> Any:
    return {
        "sub_cached_a": sub_cached_a,
        "sub_cached_b": sub_cached_b,
        "sub_uncached_a": sub_uncached_a,
        "sub_uncached_b": sub_uncached_b,
    }


@app.get("/uncached-vs-use-cache")
def get_uncached_vs_use_cache(
    no_cache: NoCacheCounterDep,
    cached_a: CachedCounterDep,
    uncached: UncachedCounterDep,
    cached_b: CachedCounterDep,
) -> Any:
    return {
        "no_cache": no_cache,
        "cached_a": cached_a,
        "uncached": uncached,
        "cached_b": cached_b,
    }


@app.get("/timeline")
def get_timeline(
    request: RequestResourceDep,
    request_b: RequestResourceDep,
    function: FunctionResourceDep,
    uncached_a: UncachedResourceDep,
    uncached_b: UncachedResourceDep,
) -> Any:
    events.append("endpoint")

    def iter_data() -> Any:
        events.append("stream-start")
        yield json.dumps(
            {
                "request_open": request.open,
                "request_same": request is request_b,
                "function_open": function.open,
                "uncached_a_open": uncached_a.open,
                "uncached_b_open": uncached_b.open,
                "uncached_same": uncached_a is uncached_b,
            }
        )
        events.append("stream-end")

    return StreamingResponse(iter_data())


@app.get("/async-generator")
def get_async_generator(
    async_a: AsyncUncachedResourceDep, async_b: AsyncUncachedResourceDep
) -> Any:
    def iter_data() -> Any:
        yield json.dumps(
            {
                "async_a_open": async_a.open,
                "async_b_open": async_b.open,
                "async_same": async_a is async_b,
            }
        )

    return StreamingResponse(iter_data())


@app.get("/class-dependency")
def get_class_dependency(
    class_a: UncachedClassDep, class_b: UncachedClassDep
) -> Any:
    return {"class_a": class_a, "class_b": class_b}


@app.get("/http-exception")
def get_http_exception(resource: UncachedResourceDep) -> Any:
    raise HTTPException(status_code=503, detail="unavailable")


@app.get("/crash")
def get_crash(resource: UncachedResourceDep) -> Any:
    raise RuntimeError("boom")


@app.get("/background")
def get_background(
    background_tasks: BackgroundTasks, resource: UncachedResourceDep
) -> Any:
    background_tasks.add_task(events.append, "background-task")
    return {"status": "ok"}


class Item(BaseModel):
    name: str


@app.get("/response-validation", response_model=Item)
def get_response_validation(resource: UncachedResourceDep) -> Any:
    return {"invalid": "data"}


@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    uncached_a: UncachedResourceDep,
    uncached_b: UncachedResourceDep,
) -> None:
    await websocket.accept()
    await websocket.send_json(
        {
            "uncached_a_open": uncached_a.open,
            "uncached_b_open": uncached_b.open,
            "uncached_same": uncached_a is uncached_b,
        }
    )
    await websocket.close()


client = TestClient(app)


def test_cache_identity() -> None:
    response = client.get("/cache-identity")
    assert response.status_code == 200
    data = response.json()
    # The default (cached) dependency is only executed once per request
    assert data["cached_a"] == data["cached_b"]
    # The uncached dependency is executed once per declaration
    assert data["uncached_a"] != data["uncached_b"]
    assert data["uncached_a"] != data["cached_a"]
    assert data["uncached_b"] != data["cached_a"]


def test_sub_cache_identity() -> None:
    response = client.get("/sub-cache-identity")
    assert response.status_code == 200
    data = response.json()
    assert data["sub_cached_a"] == data["sub_cached_b"]
    assert data["sub_uncached_a"] != data["sub_uncached_b"]


def test_uncached_does_not_populate_cache() -> None:
    response = client.get("/uncached-vs-use-cache")
    assert response.status_code == 200
    data = response.json()
    # use_cache=False stores the result in the cache, so the cached declarations
    # after it re-use the value
    assert data["no_cache"] == data["cached_a"] == data["cached_b"]
    # scope="uncached" doesn't store the result in the cache, so the cached
    # declarations after it are not affected by it
    assert data["uncached"] != data["cached_a"]


def test_timeline_scopes() -> None:
    response = client.get("/timeline")
    assert response.status_code == 200
    data = response.json()
    # The function scoped dependency exits before the response is sent
    assert data["function_open"] is False
    # The request and uncached scoped dependencies exit after the response is sent
    assert data["request_open"] is True
    assert data["uncached_a_open"] is True
    assert data["uncached_b_open"] is True
    # The cached dependency is shared, the uncached one is not
    assert data["request_same"] is True
    assert data["uncached_same"] is False
    # The uncached dependency was entered and exited once per declaration
    assert events.count("enter-uncached") == 2
    assert events.count("exit-uncached") == 2
    assert events.count("enter-request") == 1
    assert events.count("exit-request") == 1
    # The function scoped dependency exits before the response is streamed
    assert events.index("exit-function") < events.index("stream-start")
    # The request and uncached scoped dependencies exit after the response
    # was streamed
    assert events.index("exit-request") > events.index("stream-end")
    assert events.index("exit-uncached") > events.index("stream-end")


def test_async_generator_uncached() -> None:
    response = client.get("/async-generator")
    assert response.status_code == 200
    data = response.json()
    assert data["async_a_open"] is True
    assert data["async_b_open"] is True
    assert data["async_same"] is False
    assert events.count("enter-async-uncached") == 2
    assert events.count("exit-async-uncached") == 2


def test_class_dependency_uncached() -> None:
    class_dependency.calls = 0
    response = client.get("/class-dependency")
    assert response.status_code == 200
    data = response.json()
    assert data["class_a"] != data["class_b"]
    assert class_dependency.calls == 2


def test_http_exception_exits_uncached() -> None:
    response = client.get("/http-exception")
    assert response.status_code == 503
    assert response.json() == {"detail": "unavailable"}
    # The exit code of the uncached dependency still runs
    assert events.count("enter-uncached") == 1
    assert events.count("exit-uncached") == 1


def test_unhandled_exception_exits_uncached() -> None:
    with TestClient(app, raise_server_exceptions=False) as no_raise_client:
        response = no_raise_client.get("/crash")
        assert response.status_code == 500
    assert events.count("enter-uncached") == 1
    assert events.count("exit-uncached") == 1


def test_background_tasks_run_before_uncached_exit() -> None:
    response = client.get("/background")
    assert response.status_code == 200
    # Background tasks run as part of sending the response, before the
    # uncached dependency exit code
    assert events.index("background-task") < events.index("exit-uncached")


def test_response_validation_exits_uncached() -> None:
    with TestClient(app, raise_server_exceptions=False) as no_raise_client:
        response = no_raise_client.get("/response-validation")
        assert response.status_code == 500
    assert events.count("enter-uncached") == 1
    assert events.count("exit-uncached") == 1


def test_websocket_uncached() -> None:
    with client.websocket_connect("/ws") as websocket:
        data = websocket.receive_json()
    assert data["uncached_a_open"] is True
    assert data["uncached_b_open"] is True
    assert data["uncached_same"] is False
    assert events.count("enter-uncached") == 2
    assert events.count("exit-uncached") == 2


def test_dependency_overrides_preserve_uncached() -> None:
    local_app = FastAPI()
    calls = {"original": 0, "override": 0}

    def original() -> int:
        calls["original"] += 1
        return calls["original"]

    def override() -> int:
        calls["override"] += 1
        return calls["override"]

    @local_app.get("/items")
    def read_items(
        a: Annotated[int, Depends(original, scope="uncached")],
        b: Annotated[int, Depends(original, scope="uncached")],
    ) -> Any:
        return {"a": a, "b": b}

    local_app.dependency_overrides[original] = override
    local_client = TestClient(local_app)
    response = local_client.get("/items")
    assert response.status_code == 200
    data = response.json()
    assert calls["original"] == 0
    # The override keeps the uncached scope of the original dependency
    assert calls["override"] == 2
    assert data["a"] != data["b"]


def test_uncached_cannot_depend_on_function_scope() -> None:
    def uncached_parent(session: FunctionResourceDep) -> Any:
        yield session  # pragma: no cover

    with pytest.raises(
        DependencyScopeError,
        match='The dependency "uncached_parent" has a scope of "uncached", it cannot depend on dependencies with scope "function"',
    ):

        @app.get("/invalid-scope")
        def get_invalid_scope(
            resource: Annotated[Resource, Depends(uncached_parent, scope="uncached")],
        ) -> Any:  # pragma: no cover
            pass


def test_function_scope_can_depend_on_uncached() -> None:
    local_app = FastAPI()

    def function_parent(resource: UncachedResourceDep) -> Any:
        yield resource

    @local_app.get("/valid-scope")
    def get_valid_scope(
        resource: Annotated[Resource, Depends(function_parent, scope="function")],
    ) -> Any:
        return {"resource_open": resource.open}

    local_client = TestClient(local_app)
    response = local_client.get("/valid-scope")
    assert response.status_code == 200
    assert response.json() == {"resource_open": True}


@pytest.mark.anyio
async def test_async_client_uncached() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as async_client:
        response = await async_client.get("/cache-identity")
    assert response.status_code == 200
    data = response.json()
    assert data["cached_a"] == data["cached_b"]
    assert data["uncached_a"] != data["uncached_b"]


@pytest.mark.anyio
async def test_raw_asgi_call_uncached() -> None:
    sent_messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent_messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/background",
        "raw_path": b"/background",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)
    response_start = sent_messages[0]
    assert response_start["type"] == "http.response.start"
    assert response_start["status"] == 200
    # By the time the raw ASGI call finishes, all the exit code ran
    assert events.count("enter-uncached") == 1
    assert events.count("exit-uncached") == 1
    # Background tasks ran before the uncached dependency exit code
    assert events.index("background-task") < events.index("exit-uncached")
