import uuid
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI, Path, Request, WebSocket
from fastapi.testclient import TestClient
from pydantic_core import core_schema
from starlette.websockets import WebSocketDisconnect


class Slug(str):
    """A Pydantic custom type used to prove error locations stay stable."""

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: Any
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls.validate, core_schema.str_schema()
        )

    @classmethod
    def validate(cls, value: str) -> "Slug":
        if not value.islower():
            raise ValueError("Slug must be lowercase")
        return cls(value)


app = FastAPI()


@app.get("/items/{item_id}")
def read_item(item_id: str):
    return {"item_id": item_id}


@app.get("/evidence/{item_id}")
def read_evidence(request: Request, item_id: str):
    raw_path = request.scope.get("raw_path")
    return {
        "item_id": item_id,
        "raw_path": raw_path.decode() if raw_path else None,
        "path_params": request.path_params,
        "route_path": request.scope["route"].path,
        "url_for": str(request.url_for("read_evidence", item_id=item_id)),
    }


@app.get("/pair/{x}/{y}")
def read_pair(x: str, y: str):
    return {"x": x, "y": y}


@app.get("/int/{n}")
def read_int(n: int):
    return {"n": n}


@app.get("/uuid/{u}")
def read_uuid(u: uuid.UUID):
    return {"u": str(u)}


@app.get("/rx/{code}")
def read_rx(code: str = Path(pattern="^[a-z]+$")):
    return {"code": code}


@app.get("/slug/{s}")
def read_slug(s: Slug):
    return {"s": s}


@app.get("/files/{p:path}")
def read_file(p: str):
    return {"p": p}


@app.websocket("/ws/{name}")
async def websocket_endpoint(websocket: WebSocket, name: str):
    await websocket.accept()
    await websocket.send_json({"name": name})
    await websocket.close()


router = APIRouter(prefix="/v1")


@router.get("/things/{name}", name="read_thing")
def read_thing(name: str):
    return {"name": name}


app.include_router(router)

client = TestClient(app)


def test_encoded_slash_stays_within_segment():
    response = client.get("/items/a%2Fb")
    assert response.status_code == 200, response.text
    assert response.json() == {"item_id": "a/b"}


def test_encoded_slash_does_not_change_segment_boundaries():
    # %2F must not split one segment into two route parameters.
    response = client.get("/pair/a%2Fb")
    assert response.status_code == 404, response.text
    response = client.get("/pair/a%2Fb/c")
    assert response.status_code == 200, response.text
    assert response.json() == {"x": "a/b", "y": "c"}
    response = client.get("/pair/a/b")
    assert response.status_code == 200, response.text
    assert response.json() == {"x": "a", "y": "b"}


def test_unicode_param_decoded_once():
    response = client.get("/items/Caf%C3%A9")
    assert response.status_code == 200, response.text
    assert response.json() == {"item_id": "Café"}


@pytest.mark.parametrize(
    "url",
    [
        "/items/%2E",
        "/items/%2E%2E",
        "/items/%2e%2e",
        "/files/%2E%2E/secret",
        "/files/a/%2E/b",
        "/files/..%2Fsecret",
    ],
)
def test_percent_encoded_dot_segments_never_match(url: str):
    response = client.get(url)
    assert response.status_code == 404, response.text


def test_double_encoding_is_decoded_exactly_once():
    response = client.get("/items/%252F")
    assert response.status_code == 200, response.text
    assert response.json() == {"item_id": "%2F"}


def test_invalid_utf8_does_not_bypass_constraints():
    response = client.get("/items/%FF")
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["type"] == "path_param_decode"
    assert error["loc"] == ["path", "item_id"]
    assert error["input"] == "%FF"
    # The error message states the adopted decoded-exactly-once semantics.
    assert "percent-decoded exactly once" in error["msg"]


def test_invalid_utf8_error_location_stable_for_typed_params():
    response = client.get("/int/%FF")
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["type"] == "path_param_decode"
    assert error["loc"] == ["path", "n"]


def test_int_error_location_stable():
    response = client.get("/int/xy")
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["type"] == "int_parsing"
    assert error["loc"] == ["path", "n"]
    assert error["input"] == "xy"


def test_uuid_error_location_stable():
    response = client.get("/uuid/not-a-uuid")
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["loc"] == ["path", "u"]
    assert error["input"] == "not-a-uuid"


def test_path_regex_validates_decoded_value():
    # The raw value is ABC%20 (invalid for the pattern either way); the
    # error input shows the decoded value, documenting the semantics.
    response = client.get("/rx/ABC%20")
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["type"] == "string_pattern_mismatch"
    assert error["loc"] == ["path", "code"]
    assert error["input"] == "ABC "


def test_pydantic_custom_type_error_location_stable():
    response = client.get("/slug/ABC%2F")
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["loc"] == ["path", "s"]
    # The custom type validated the decoded value "ABC/".
    assert error["input"] == "ABC/"


def test_url_path_for_encodes_with_same_rules():
    assert app.url_path_for("read_item", item_id="a/b") == "/items/a%2Fb"
    assert app.url_path_for("read_item", item_id="Café") == "/items/Caf%C3%A9"
    assert app.url_path_for("read_item", item_id="%2F") == "/items/%252F"
    assert app.url_path_for("read_item", item_id="a b") == "/items/a%20b"
    assert app.url_path_for("read_file", p="a/b café") == "/files/a/b%20caf%C3%A9"


@pytest.mark.parametrize(
    "value",
    ["a/b", "Café", "%2F", "a b", "100%", "a?b#c"],
)
def test_url_for_round_trip(value: str):
    # url_for -> raw URL -> route match -> decode yields the same value.
    url = app.url_path_for("read_item", item_id=value)
    response = client.get(url)
    assert response.status_code == 200, response.text
    assert response.json() == {"item_id": value}


def test_round_trip_evidence_chain():
    # Raw scope, decoded params, matched route and url_for agree with
    # each other for an encoded-slash + Unicode URL.
    response = client.get("/evidence/a%2FCaf%C3%A9")
    assert response.status_code == 200, response.text
    evidence = response.json()
    assert evidence["raw_path"] == "/evidence/a%2FCaf%C3%A9"
    assert evidence["item_id"] == "a/Café"
    assert evidence["path_params"] == {"item_id": "a/Café"}
    assert evidence["route_path"] == "/evidence/{item_id}"
    assert evidence["url_for"].endswith("/evidence/a%2FCaf%C3%A9")


def test_included_router_uses_same_semantics():
    response = client.get("/v1/things/a%2Fb")
    assert response.status_code == 200, response.text
    assert response.json() == {"name": "a/b"}
    assert app.url_path_for("read_thing", name="a/b") == "/v1/things/a%2Fb"


def test_websocket_param_decoded_once():
    with client.websocket_connect("/ws/Caf%C3%A9") as websocket:
        assert websocket.receive_json() == {"name": "Café"}


def test_websocket_invalid_utf8_rejected():
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/%FF"):
            pass  # pragma: no cover
    assert exc_info.value.code == 1008


def test_root_path_proxy_semantics():
    proxy_app = FastAPI(root_path="/api/v1")

    @proxy_app.get("/items/{item_id}")
    def read_proxied_item(item_id: str):
        return {"item_id": item_id}

    proxy_client = TestClient(proxy_app)
    response = proxy_client.get("/items/a%2Fb")
    assert response.status_code == 200, response.text
    assert response.json() == {"item_id": "a/b"}
    response = proxy_client.get("/items/%FF")
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["loc"] == ["path", "item_id"]


def test_trailing_slash_redirect_keeps_encoding():
    slash_app = FastAPI()

    @slash_app.get("/slash/{value}/")
    def read_slash(value: str):
        return {"value": value}

    slash_client = TestClient(slash_app)
    response = slash_client.get("/slash/a%2Fb", follow_redirects=False)
    assert response.status_code == 307, response.text
    assert response.headers["location"].endswith("/slash/a%2Fb/")
    response = slash_client.get("/slash/a%2Fb")
    assert response.status_code == 200, response.text
    assert response.json() == {"value": "a/b"}


def test_mounted_sub_application_uses_same_semantics():
    outer = FastAPI()
    sub = FastAPI()

    @sub.get("/things/{name}")
    def read_sub_thing(name: str):
        return {"name": name}

    outer.mount("/sub", sub)
    mounted_client = TestClient(outer)
    response = mounted_client.get("/sub/things/a%2Fb")
    assert response.status_code == 200, response.text
    assert response.json() == {"name": "a/b"}
    response = mounted_client.get("/sub/things/%2E%2E")
    assert response.status_code == 404, response.text
