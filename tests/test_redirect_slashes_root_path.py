import pytest

from fastapi import APIRouter, FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route, Router


def make_app() -> FastAPI:
    app = FastAPI()
    inner_router = APIRouter()
    outer_router = APIRouter()

    @app.get("/users/")
    def read_users(request: Request) -> dict[str, object]:
        return {
            "method": request.method,
            "root_path": request.scope["root_path"],
            "path": request.scope["path"],
            "query": str(request.url.query),
        }

    @app.post("/users/")
    def create_user(request: Request) -> dict[str, object]:
        return {
            "method": request.method,
            "root_path": request.scope["root_path"],
            "path": request.scope["path"],
            "query": str(request.url.query),
        }

    @app.get("/profile")
    def read_profile(request: Request) -> dict[str, object]:
        return {
            "method": request.method,
            "root_path": request.scope["root_path"],
            "path": request.scope["path"],
        }

    @app.post("/profile")
    def create_profile(request: Request) -> dict[str, object]:
        return {
            "method": request.method,
            "root_path": request.scope["root_path"],
            "path": request.scope["path"],
        }

    @app.get("/items/{item_id}/")
    def read_item(item_id: str, request: Request) -> dict[str, object]:
        return {
            "item_id": item_id,
            "root_path": request.scope["root_path"],
            "path": request.scope["path"],
        }

    @inner_router.websocket("/ws/")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_json(
            {
                "root_path": websocket.scope["root_path"],
                "path": websocket.scope["path"],
                "query": str(websocket.url.query),
            }
        )
        await websocket.close()

    outer_router.include_router(inner_router, prefix="/inner")
    app.include_router(outer_router, prefix="/outer")
    nested_router = APIRouter(prefix="/native")

    @nested_router.post("/users/")
    def create_nested_user(request: Request) -> dict[str, object]:
        return {
            "method": request.method,
            "root_path": request.scope["root_path"],
            "path": request.scope["path"],
            "query": str(request.url.query),
        }

    app.include_router(nested_router, prefix="/included")
    return app


def test_redirect_includes_root_path_scheme_host_encoding_and_query() -> None:
    client = TestClient(make_app(), root_path="/gateway")

    response = client.get(
        "/items/%5Ba%20b%5D?next=%2Fusers%2F%3Fx%3D1%26y%3D2",
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == (
        "http://testserver/gateway/items/%5Ba%20b%5D/"
        "?next=%2Fusers%2F%3Fx%3D1%26y%3D2"
    )

    client_without_root_path = TestClient(make_app())
    response = client_without_root_path.get(
        "/items/%5Ba%20b%5D?next=%2Fusers%2F",
        follow_redirects=False,
    )

    assert response.headers["location"] == (
        "http://testserver/items/%5Ba%20b%5D/?next=%2Fusers%2F"
    )


def test_redirect_preserves_post_method_and_body_when_followed() -> None:
    client = TestClient(make_app(), root_path="/gateway")

    response = client.post("/users?active=true", json={"name": "Alice"})

    assert response.status_code == 200
    assert response.request.method == "POST"
    assert response.json() == {
        "method": "POST",
        "root_path": "/gateway",
        "path": "/gateway/users/",
        "query": "active=true",
    }


@pytest.mark.anyio
async def test_redirect_does_not_consume_body_and_uses_standard_proxy_scope() -> None:
    requests: list[tuple[str, str, str, bytes]] = []
    app = FastAPI()

    @app.post("/users/")
    async def create_user(request: Request) -> dict[str, bool]:
        requests.append(
            (
                request.method,
                request.scope["root_path"],
                request.scope["path"],
                await request.body(),
            )
        )
        return {"ok": True}

    body = b'{"name":"Alice"}'
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/users",
        "raw_path": b"/users",
        "root_path": "/gateway",
        "scheme": "https",
        "server": ["example.com", 443],
        "query_string": b"active=true",
        "headers": [
            (b"host", b"example.com"),
            (b"content-length", str(len(body)).encode()),
        ],
        "app": app,
    }
    messages = []
    receives = 0

    async def receive() -> dict[str, object]:
        nonlocal receives
        receives += 1
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    await app(scope, receive, send)

    assert messages[0]["status"] == 307
    assert (
        b"location",
        b"https://example.com/gateway/users/?active=true",
    ) in messages[0]["headers"]
    assert requests == []
    assert receives == 0


def test_nested_router_redirect_includes_effective_prefix_and_root_path() -> None:
    client = TestClient(make_app(), root_path="/gateway")

    response = client.get("/outer/inner/ws?token=abc", follow_redirects=False)

    assert response.status_code == 404
    assert "location" not in response.headers

    with client.websocket_connect("/outer/inner/ws/?token=abc") as websocket:
        assert websocket.receive_json() == {
            "root_path": "/gateway",
            "path": "/outer/inner/ws/",
            "query": "token=abc",
        }


def test_nested_router_prefixes_apply_to_post_redirect() -> None:
    client = TestClient(make_app(), root_path="/gateway")

    response = client.post("/included/native/users?active=true")

    assert response.status_code == 200
    assert response.request.method == "POST"
    assert response.json() == {
        "method": "POST",
        "root_path": "/gateway",
        "path": "/gateway/included/native/users/",
        "query": "active=true",
    }


def test_redirect_removes_slash_for_post_with_root_path_and_prefix() -> None:
    client = TestClient(make_app(), root_path="/gateway")

    response = client.post("/profile/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "http://testserver/gateway/profile"

    response = client.post("/profile/")

    assert response.status_code == 200
    assert response.request.method == "POST"
    assert response.json() == {
        "method": "POST",
        "root_path": "/gateway",
        "path": "/gateway/profile",
    }


def test_redirect_slashes_disabled_does_not_match_or_redirect() -> None:
    app = FastAPI(redirect_slashes=False)

    @app.post("/users/")
    def create_user() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app, root_path="/gateway")
    response = client.post("/users?active=true", follow_redirects=False)

    assert response.status_code == 404
    assert "location" not in response.headers


def test_redirect_slashes_disabled_still_matches_exact_nested_route() -> None:
    app = FastAPI(redirect_slashes=False)
    router = APIRouter(prefix="/native")

    @router.post("/users/")
    def create_user(request: Request) -> dict[str, str]:
        return {
            "root_path": request.scope["root_path"],
            "path": request.scope["path"],
        }

    app.include_router(router, prefix="/included")
    client = TestClient(app, root_path="/gateway")

    response = client.post("/included/native/users/")

    assert response.status_code == 200
    assert response.json() == {
        "root_path": "/gateway",
        "path": "/included/native/users/",
    }


def test_openapi_server_uses_request_root_path() -> None:
    client = TestClient(make_app(), root_path="/gateway")

    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["servers"] == [{"url": "/gateway"}]


def test_configured_root_path_is_used_when_scope_root_path_is_empty() -> None:
    app = FastAPI(root_path="/configured")

    @app.post("/users/")
    def create_user(request: Request) -> dict[str, str]:
        return {"root_path": request.scope["root_path"]}

    response = TestClient(app).post("/users", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "http://testserver/configured/users/"

    response = TestClient(app, root_path="/gateway").post(
        "/users", follow_redirects=False
    )

    assert response.status_code == 307
    assert response.headers["location"] == "http://testserver/gateway/users/"


def test_starlette_mount_redirect_scope_stays_inside_mount() -> None:
    async def mounted_users(request: Request) -> PlainTextResponse:
        return PlainTextResponse(
            f"{request.scope['root_path']} {request.scope['path']} {request.method}"
        )

    mounted = Router(
        routes=[Route("/users/", mounted_users, methods=["GET", "POST"])]
    )
    app = FastAPI(routes=[Mount("/api", app=mounted)])
    client = TestClient(app, root_path="/gateway")

    response = client.post("/gateway/api/users?x=1", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "http://testserver/gateway/api/users/?x=1"

    response = client.post("/gateway/api/users?x=1")
    assert response.status_code == 200
    assert response.text == "/gateway/api /gateway/api/users/ POST"
