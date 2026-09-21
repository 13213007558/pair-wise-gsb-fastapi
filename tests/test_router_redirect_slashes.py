import pytest
from fastapi import APIRouter, FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect


def test_redirect_slashes_enabled():
    app = FastAPI()
    router = APIRouter()

    @router.get("/hello/")
    def hello_page() -> str:
        return "Hello, World!"

    app.include_router(router)

    client = TestClient(app)

    response = client.get("/hello/", follow_redirects=False)
    assert response.status_code == 200

    response = client.get("/hello", follow_redirects=False)
    assert response.status_code == 307


def test_redirect_slashes_disabled():
    app = FastAPI(redirect_slashes=False)
    router = APIRouter()

    @router.get("/hello/")
    def hello_page() -> str:
        return "Hello, World!"

    app.include_router(router)

    client = TestClient(app)

    response = client.get("/hello/", follow_redirects=False)
    assert response.status_code == 200

    response = client.get("/hello", follow_redirects=False)
    assert response.status_code == 404


def build_root_path_app(**kwargs) -> FastAPI:
    app = FastAPI(root_path="/gateway", **kwargs)

    @app.get("/users/")
    def get_users() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/users/")
    def create_user() -> dict[str, bool]:
        return {"created": True}

    return app


def test_redirect_slashes_keeps_root_path_prefix():
    client = TestClient(build_root_path_app())

    response = client.get("/users", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "http://testserver/gateway/users/"

    # Both the stripped path (as forwarded by the proxy) and the full
    # external path reach the same handler.
    response = client.get("/users/", follow_redirects=False)
    assert response.status_code == 200
    response = client.get("/gateway/users/", follow_redirects=False)
    assert response.status_code == 200


def test_redirect_slashes_root_path_strips_trailing_slash():
    app = FastAPI(root_path="/gateway")

    @app.get("/items")
    def get_items() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app)
    response = client.get("/items/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "http://testserver/gateway/items"


def test_redirect_slashes_root_path_keeps_query_string():
    client = TestClient(build_root_path_app())

    response = client.get("/users?skip=0&limit=10", follow_redirects=False)
    assert response.status_code == 307
    assert (
        response.headers["location"]
        == "http://testserver/gateway/users/?skip=0&limit=10"
    )


def test_redirect_slashes_root_path_post_keeps_method():
    client = TestClient(build_root_path_app())

    response = client.post("/users", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "http://testserver/gateway/users/"

    followed = client.post("/users", follow_redirects=True)
    assert followed.status_code == 200
    assert followed.json() == {"created": True}
    assert followed.request.method == "POST"


def test_redirect_slashes_root_path_keeps_scheme_and_host():
    client = TestClient(build_root_path_app())

    response = client.get("https://api.example.com/users", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "https://api.example.com/gateway/users/"


def test_redirect_slashes_root_path_encodes_path():
    app = FastAPI(root_path="/gateway")

    @app.get("/café/")
    def cafe() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app)
    response = client.get("/caf%C3%A9", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "http://testserver/gateway/caf%C3%A9/"


def test_redirect_slashes_server_root_path_full_path():
    # The server/proxy keeps the root_path prefix in the path, as uvicorn
    # does with --root-path.
    client = TestClient(build_root_path_app(), root_path="/gateway")

    response = client.get("/gateway/users", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "http://testserver/gateway/users/"

    followed = client.get("/gateway/users", follow_redirects=True)
    assert followed.status_code == 200
    assert followed.json() == {"ok": True}


def test_redirect_slashes_server_root_path_stripped_path():
    # The proxy strips the prefix before forwarding, the path doesn't
    # include the root_path.
    app = FastAPI()

    @app.get("/users/")
    def get_users() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app, root_path="/gateway")
    response = client.get("/users", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "http://testserver/gateway/users/"


def test_redirect_slashes_nested_router_prefix_root_path():
    app = FastAPI(root_path="/gateway")
    api = APIRouter(prefix="/api")
    v1 = APIRouter(prefix="/v1")

    @v1.get("/items/")
    def list_items() -> dict[str, list]:
        return {"items": []}

    api.include_router(v1)
    app.include_router(api, prefix="/private")

    client = TestClient(app)
    response = client.get("/private/api/v1/items", follow_redirects=False)
    assert response.status_code == 307
    assert (
        response.headers["location"]
        == "http://testserver/gateway/private/api/v1/items/"
    )

    followed = client.get("/private/api/v1/items", follow_redirects=True)
    assert followed.status_code == 200
    assert followed.json() == {"items": []}


def test_redirect_slashes_mounted_app_root_path():
    app = FastAPI(root_path="/gateway")
    sub_app = FastAPI()

    @sub_app.get("/items/")
    def list_items() -> dict[str, list]:
        return {"items": []}

    app.mount("/sub", sub_app)

    client = TestClient(app)
    response = client.get("/sub/items", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "http://testserver/gateway/sub/items/"

    followed = client.get("/sub/items", follow_redirects=True)
    assert followed.status_code == 200
    assert followed.json() == {"items": []}

    direct = client.get("/sub/items/", follow_redirects=False)
    assert direct.status_code == 200


def test_redirect_slashes_websocket_root_path():
    app = FastAPI(root_path="/gateway")

    @app.websocket("/ws/")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"ok": True})
        await websocket.close()

    client = TestClient(app)
    with pytest.raises(WebSocketDenialResponse) as exc_info:
        with client.websocket_connect("/ws"):
            pass  # pragma: no cover
    denial = exc_info.value
    assert denial.status_code == 307
    assert denial.headers["location"] == "ws://testserver/gateway/ws/"

    with client.websocket_connect("/ws/") as websocket:
        assert websocket.receive_json() == {"ok": True}


def test_redirect_slashes_disabled_websocket():
    app = FastAPI(root_path="/gateway", redirect_slashes=False)

    @app.websocket("/ws/")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        await websocket.close()

    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass  # pragma: no cover


def test_redirect_slashes_disabled_root_path():
    app = build_root_path_app(redirect_slashes=False)
    client = TestClient(app)

    response = client.get("/users", follow_redirects=False)
    assert response.status_code == 404

    response = client.get("/gateway/users/", follow_redirects=False)
    assert response.status_code == 200


def test_redirect_slashes_openapi_servers_match_root_path():
    client = TestClient(build_root_path_app())

    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert response.json()["servers"] == [{"url": "/gateway"}]
