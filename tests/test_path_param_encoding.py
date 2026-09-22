import uuid
from typing import Annotated

import pytest
from fastapi import APIRouter, FastAPI, Path, Request, WebSocket
from fastapi.path_params import PathParamEncodeError
from fastapi.testclient import TestClient
from pydantic import AfterValidator
from starlette.websockets import WebSocketDisconnect

app = FastAPI()


@app.get("/items/{item_id}")
def get_item(item_id: str):
    return {"item_id": item_id}


@app.get("/items/{item_id}/sub")
def get_item_sub(item_id: str):
    return {"item_id": item_id}


@app.get("/two/{first}/{second}")
def get_two(first: str, second: str):
    return {"first": first, "second": second}


@app.get("/int/{n}")
def get_int(n: int):
    return {"n": n}


@app.get("/uuid/{uid}")
def get_uuid(uid: uuid.UUID):
    return {"uid": str(uid)}


@app.get("/regex/{x}")
def get_regex(x: str = Path(pattern="^[a-z]+$")):
    return {"x": x}


def _no_uppercase(value: str) -> str:
    if value != value.lower():
        raise ValueError("must be lowercase")
    return value


LowerStr = Annotated[str, AfterValidator(_no_uppercase)]


@app.get("/custom/{x}")
def get_custom(x: LowerStr):
    return {"x": x}


@app.get("/files/{file_path:path}")
def get_file(file_path: str):
    return {"file_path": file_path}


@app.get("/evidence/{item_id}")
def get_evidence(item_id: str, request: Request):
    return {
        "raw_path": request.scope["raw_path"].decode("ascii"),
        "raw_path_params": request.scope["fastapi_raw_path_params"],
        "path_params": request.path_params,
        "route_path": request.scope["route"].path,
        "url_for": app.url_path_for("get_evidence", item_id=item_id),
    }


@app.get("/slash/{x}/")
def get_slash(x: str):
    return {"x": x}


@app.websocket("/ws/{room}")
async def websocket_room(websocket: WebSocket, room: str):
    await websocket.accept()
    await websocket.send_json({"room": room})
    await websocket.close()


router = APIRouter(prefix="/v1")


@router.get("/users/{name}")
def get_user(name: str):
    return {"name": name}


app.include_router(router)

client = TestClient(app)


def test_encoded_slash_stays_within_segment():
    response = client.get("/items/a%2Fb")
    assert response.status_code == 200, response.text
    assert response.json() == {"item_id": "a/b"}


def test_encoded_slash_does_not_match_multi_segment_route():
    # %2F must not turn one segment into two
    response = client.get("/two/x%2Fy")
    assert response.status_code == 404, response.text


def test_encoded_slash_does_not_change_segment_boundaries():
    # /items/{item_id}/sub must not see an extra segment from %2F
    response = client.get("/items/a%2Fb/sub")
    assert response.status_code == 200, response.text
    assert response.json() == {"item_id": "a/b"}
    # and the decoded value must not leak into the "sub" position
    response = client.get("/items/a%2Fsub")
    assert response.status_code == 200, response.text
    assert response.json() == {"item_id": "a/sub"}


def test_unicode_param_decoded_once():
    response = client.get("/items/caf%C3%A9")
    assert response.status_code == 200, response.text
    assert response.json() == {"item_id": "café"}


def test_percent_encoded_dot_segments_rejected():
    for url in ["/items/%2E", "/items/%2E%2E", "/items/.%2E", "/items/%2E."]:
        response = client.get(url)
        assert response.status_code == 422, (url, response.text)
        (error,) = response.json()["detail"]
        assert error["type"] == "path_param_decode"
        assert error["loc"] == ["path", "item_id"]
        assert error["ctx"]["semantics"] == "decoded"
        assert "decoded value" in error["msg"]


def test_percent_encoded_dot_segments_rejected_in_path_convertor():
    for url in ["/files/%2E%2E/etc", "/files/a/%2E/b", "/files/%2E%2E"]:
        response = client.get(url)
        assert response.status_code == 422, (url, response.text)
        (error,) = response.json()["detail"]
        assert error["type"] == "path_param_decode"
        assert error["loc"] == ["path", "file_path"]
        assert error["ctx"]["semantics"] == "decoded"


def test_double_encoding_decoded_exactly_once():
    response = client.get("/items/%252F")
    assert response.status_code == 200, response.text
    assert response.json() == {"item_id": "%2F"}
    # %252E%252E decodes once to the harmless literal "%2E%2E", not to ".."
    response = client.get("/items/%252E%252E")
    assert response.status_code == 200, response.text
    assert response.json() == {"item_id": "%2E%2E"}


def test_invalid_utf8_rejected():
    response = client.get("/items/%FF%FE")
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["type"] == "path_param_decode"
    assert error["loc"] == ["path", "item_id"]
    assert error["input"] == "%FF%FE"
    assert error["ctx"]["semantics"] == "raw"
    assert "raw" in error["msg"]


def test_invalid_percent_escape_rejected():
    for url in ["/items/%ZZ", "/items/%", "/items/%1", "/items/100%"]:
        response = client.get(url)
        assert response.status_code == 422, (url, response.text)
        (error,) = response.json()["detail"]
        assert error["type"] == "path_param_decode"
        assert error["loc"] == ["path", "item_id"]
        assert error["ctx"]["semantics"] == "raw"


def test_literal_percent_decoded_from_escape():
    response = client.get("/items/100%25")
    assert response.status_code == 200, response.text
    assert response.json() == {"item_id": "100%"}


def test_int_param_error_location_stable():
    response = client.get("/int/foobar")
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["type"] == "int_parsing"
    assert error["loc"] == ["path", "n"]


def test_uuid_param_error_location_stable():
    response = client.get("/uuid/not-a-uuid")
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["loc"] == ["path", "uid"]
    valid = "123e4567-e89b-12d3-a456-426614174000"
    response = client.get(f"/uuid/{valid}")
    assert response.status_code == 200, response.text
    assert response.json() == {"uid": valid}


def test_regex_constraint_applies_to_decoded_value():
    response = client.get("/regex/abc")
    assert response.status_code == 200, response.text
    assert response.json() == {"x": "abc"}
    # The raw segment "ABC%31" decodes to "ABC1", which violates the pattern
    response = client.get("/regex/ABC%31")
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["loc"] == ["path", "x"]
    assert error["input"] == "ABC1"


def test_pydantic_custom_type_error_location_stable():
    response = client.get("/custom/ABC")
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["loc"] == ["path", "x"]
    assert error["input"] == "ABC"
    response = client.get("/custom/abc")
    assert response.status_code == 200, response.text
    assert response.json() == {"x": "abc"}


def test_url_for_encodes_with_same_rules():
    assert app.url_path_for("get_item", item_id="a/b") == "/items/a%2Fb"
    assert app.url_path_for("get_item", item_id="café") == "/items/caf%C3%A9"
    assert app.url_path_for("get_item", item_id="100%") == "/items/100%25"
    assert app.url_path_for("get_item", item_id="%2F") == "/items/%252F"
    assert app.url_path_for("get_file", file_path="a/b/c") == "/files/a/b/c"
    assert app.url_path_for("get_file", file_path="a b") == "/files/a%20b"
    assert app.url_path_for("get_int", n=42) == "/int/42"


def test_url_for_rejects_dot_segments():
    with pytest.raises(PathParamEncodeError):
        app.url_path_for("get_item", item_id="..")
    with pytest.raises(PathParamEncodeError):
        app.url_path_for("get_file", file_path="a/../b")


def test_url_for_round_trip():
    for value in ["a/b", "café", "100%", "%2F", "a b", "x@y:z"]:
        url = app.url_path_for("get_item", item_id=value)
        response = client.get(url)
        assert response.status_code == 200, (value, url, response.text)
        assert response.json() == {"item_id": value}


def test_trailing_slash_redirect():
    response = client.get("/slash/abc", follow_redirects=False)
    assert response.status_code == 307, response.text
    assert response.headers["location"] == "http://testserver/slash/abc/"
    response = client.get("/slash/abc", follow_redirects=True)
    assert response.status_code == 200, response.text
    assert response.json() == {"x": "abc"}


def test_trailing_slash_redirect_preserves_encoding():
    response = client.get("/slash/a%2Fb", follow_redirects=False)
    assert response.status_code == 307, response.text
    assert response.headers["location"] == "http://testserver/slash/a%2Fb/"
    response = client.get("/slash/a%2Fb", follow_redirects=True)
    assert response.status_code == 200, response.text
    assert response.json() == {"x": "a/b"}


def test_root_path():
    root_client = TestClient(app, root_path="/api/v1")
    response = root_client.get("/items/a%2Fb")
    assert response.status_code == 200, response.text
    assert response.json() == {"item_id": "a/b"}
    response = root_client.get("/items/%2E%2E")
    assert response.status_code == 422, response.text


def test_included_router_with_prefix():
    response = client.get("/v1/users/a%2Fb")
    assert response.status_code == 200, response.text
    assert response.json() == {"name": "a/b"}
    assert app.url_path_for("get_user", name="a/b") == "/v1/users/a%2Fb"


def test_websocket_param_decoded():
    with client.websocket_connect("/ws/a%2Fb") as websocket:
        assert websocket.receive_json() == {"room": "a/b"}


def test_websocket_param_decode_error_rejected():
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/%2E%2E"):
            pass
    assert exc_info.value.code == 1008


def test_round_trip_evidence_chain():
    response = client.get("/evidence/a%2Fb")
    assert response.status_code == 200, response.text
    evidence = response.json()
    # The raw scope, the raw and decoded parameters, the matched route and
    # the url_for() output all describe the same value under the same rules.
    assert evidence["raw_path"] == "/evidence/a%2Fb"
    assert evidence["raw_path_params"] == {"item_id": "a%2Fb"}
    assert evidence["path_params"] == {"item_id": "a/b"}
    assert evidence["route_path"] == "/evidence/{item_id}"
    assert evidence["url_for"] == "/evidence/a%2Fb"


def test_round_trip_evidence_chain_unicode():
    response = client.get("/evidence/caf%C3%A9")
    assert response.status_code == 200, response.text
    evidence = response.json()
    assert evidence["raw_path"] == "/evidence/caf%C3%A9"
    assert evidence["raw_path_params"] == {"item_id": "caf%C3%A9"}
    assert evidence["path_params"] == {"item_id": "café"}
    assert evidence["url_for"] == "/evidence/caf%C3%A9"
