from typing import Optional

from fastapi import FastAPI, Form, Query
from fastapi.testclient import TestClient
from pydantic import BaseModel

app = FastAPI()

URLENCODED = {"Content-Type": "application/x-www-form-urlencoded"}


class Item(BaseModel):
    name: str


class TokenForm(BaseModel):
    token: str


@app.post("/scalar")
async def post_scalar(q: str = Form()):
    return {"q": q}


@app.post("/scalar-strict")
async def post_scalar_strict(q: str = Form(strict=True)):
    return {"q": q}


@app.post("/int-strict")
async def post_int_strict(q: int = Form(strict=True)):
    return {"q": q}


@app.post("/list")
async def post_list(q: list[int] = Form()):
    return {"q": q}


@app.post("/list-default")
async def post_list_default(q: list[int] = Form([])):
    return {"q": q}


@app.post("/set")
async def post_set(q: set[int] = Form()):
    return {"q": sorted(q)}


@app.post("/tuple")
async def post_tuple(q: tuple[int, ...] = Form()):
    return {"q": list(q)}


@app.post("/optional")
async def post_optional(q: Optional[str] = Form(None)):
    return {"q": q}


@app.post("/optional-list")
async def post_optional_list(q: Optional[list[int]] = Form(None)):
    return {"q": q}


@app.post("/default")
async def post_default(q: str = Form("default")):
    return {"q": q}


@app.post("/aliased")
async def post_aliased(user_name: str = Form(alias="user-name")):
    return {"user_name": user_name}


@app.get("/query-scalar")
async def get_query_scalar(q: str = Query()):
    return {"q": q}


@app.get("/query-list")
async def get_query_list(q: list[int] = Query()):
    return {"q": q}


@app.post("/mixed")
async def post_mixed(
    query_token: str = Query(alias="token"),
    form_token: str = Form(alias="token"),
):
    return {"query_token": query_token, "form_token": form_token}


@app.post("/mixed-model")
async def post_mixed_model(token: str = Query(), form: TokenForm = Form()):
    return {"query_token": token, "form_token": form.token}


@app.post("/json-body")
async def post_json_body(item: Item):
    return item


client = TestClient(app)


def post_form(url: str, body: str):
    return client.post(url, content=body, headers=URLENCODED)


def test_scalar_single_value():
    response = post_form("/scalar", "q=a")
    assert response.status_code == 200, response.text
    assert response.json() == {"q": "a"}


def test_scalar_repeated_last_value_wins():
    response = post_form("/scalar", "q=a&q=b")
    assert response.status_code == 200, response.text
    assert response.json() == {"q": "b"}


def test_scalar_repeated_trailing_empty_is_ignored():
    response = post_form("/scalar", "q=a&q=")
    assert response.status_code == 200, response.text
    assert response.json() == {"q": "a"}


def test_scalar_repeated_leading_empty_is_ignored():
    response = post_form("/scalar", "q=&q=a")
    assert response.status_code == 200, response.text
    assert response.json() == {"q": "a"}


def test_scalar_empty_string_is_missing():
    response = post_form("/scalar", "q=")
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["type"] == "missing"
    assert response.json()["detail"][0]["loc"] == ["body", "q"]


def test_scalar_missing():
    response = client.post("/scalar")
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["type"] == "missing"
    assert response.json()["detail"][0]["loc"] == ["body", "q"]


def test_strict_scalar_single_value():
    response = post_form("/scalar-strict", "q=a")
    assert response.status_code == 200, response.text
    assert response.json() == {"q": "a"}


def test_strict_scalar_repeated_is_rejected():
    response = post_form("/scalar-strict", "q=a&q=b")
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["type"] == "multiple_values"
    assert error["loc"] == ["body", "q"]
    assert error["msg"] == "Expected a single value, got 2 values"
    assert error["input"] == ["a", "b"]
    assert error["ctx"] == {"count": 2}


def test_strict_scalar_repeated_with_empty_string():
    # Empty strings are treated as missing, so only one real value remains
    response = post_form("/scalar-strict", "q=&q=a")
    assert response.status_code == 200, response.text
    assert response.json() == {"q": "a"}


def test_strict_int_still_parses_strings():
    # `strict` controls multi-value handling, it must not enable Pydantic's
    # strict mode, which would reject all non-str form input
    response = post_form("/int-strict", "q=1")
    assert response.status_code == 200, response.text
    assert response.json() == {"q": 1}


def test_strict_int_repeated_is_rejected():
    response = post_form("/int-strict", "q=1&q=2")
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["type"] == "multiple_values"
    assert error["loc"] == ["body", "q"]


def test_list_repeated_preserves_order():
    response = post_form("/list", "q=3&q=1&q=2")
    assert response.status_code == 200, response.text
    assert response.json() == {"q": [3, 1, 2]}


def test_list_single_value():
    response = post_form("/list", "q=1")
    assert response.status_code == 200, response.text
    assert response.json() == {"q": [1]}


def test_list_empty_strings_are_dropped():
    response = post_form("/list", "q=1&q=&q=2")
    assert response.status_code == 200, response.text
    assert response.json() == {"q": [1, 2]}


def test_list_only_empty_string_is_missing():
    response = post_form("/list", "q=")
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["type"] == "missing"
    assert response.json()["detail"][0]["loc"] == ["body", "q"]


def test_list_default_with_empty_string():
    response = post_form("/list-default", "q=")
    assert response.status_code == 200, response.text
    assert response.json() == {"q": []}


def test_list_missing_required():
    response = client.post("/list")
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["type"] == "missing"
    assert response.json()["detail"][0]["loc"] == ["body", "q"]


def test_list_invalid_item_error_location():
    response = post_form("/list", "q=1&q=x")
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["type"] == "int_parsing"
    assert error["loc"] == ["body", "q", 1]
    assert error["input"] == "x"


def test_set_repeated_values():
    response = post_form("/set", "q=2&q=1&q=2")
    assert response.status_code == 200, response.text
    assert response.json() == {"q": [1, 2]}


def test_tuple_repeated_values():
    response = post_form("/tuple", "q=2&q=1")
    assert response.status_code == 200, response.text
    assert response.json() == {"q": [2, 1]}


def test_optional_empty_string_is_default():
    response = post_form("/optional", "q=")
    assert response.status_code == 200, response.text
    assert response.json() == {"q": None}


def test_optional_missing_is_default():
    response = client.post("/optional")
    assert response.status_code == 200, response.text
    assert response.json() == {"q": None}


def test_optional_with_value():
    response = post_form("/optional", "q=a")
    assert response.status_code == 200, response.text
    assert response.json() == {"q": "a"}


def test_optional_list_empty_string_is_default():
    response = post_form("/optional-list", "q=")
    assert response.status_code == 200, response.text
    assert response.json() == {"q": None}


def test_optional_list_repeated_values():
    response = post_form("/optional-list", "q=1&q=2")
    assert response.status_code == 200, response.text
    assert response.json() == {"q": [1, 2]}


def test_default_with_empty_string():
    response = post_form("/default", "q=")
    assert response.status_code == 200, response.text
    assert response.json() == {"q": "default"}


def test_alias_is_used_for_extraction():
    response = post_form("/aliased", "user-name=ada")
    assert response.status_code == 200, response.text
    assert response.json() == {"user_name": "ada"}


def test_alias_is_used_in_error_location():
    response = post_form("/aliased", "user_name=ada")
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["type"] == "missing"
    assert error["loc"] == ["body", "user-name"]


def test_query_scalar_repeated_unchanged():
    response = client.get("/query-scalar", params=[("q", "a"), ("q", "b")])
    assert response.status_code == 200, response.text
    assert response.json() == {"q": "b"}


def test_query_list_repeated_unchanged():
    response = client.get("/query-list", params=[("q", "1"), ("q", "2")])
    assert response.status_code == 200, response.text
    assert response.json() == {"q": [1, 2]}


def test_query_and_form_field_with_same_alias_dont_overlap():
    response = client.post(
        "/mixed?token=from-query", content="token=from-form", headers=URLENCODED
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "query_token": "from-query",
        "form_token": "from-form",
    }


def test_query_param_and_form_model_field_with_same_name_dont_overlap():
    response = client.post(
        "/mixed-model?token=from-query",
        content="token=from-form",
        headers=URLENCODED,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "query_token": "from-query",
        "form_token": "from-form",
    }


def test_json_content_type_sent_to_form_endpoint():
    response = client.post("/scalar", json={"q": "a"})
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["type"] == "missing"
    assert error["loc"] == ["body", "q"]


def test_form_content_type_sent_to_json_endpoint():
    response = post_form("/json-body", "name=ada")
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["loc"] == ["body"]


def test_multipart_scalar_repeated_last_value_wins():
    response = client.post(
        "/scalar", files=[("q", (None, "a")), ("q", (None, "b"))]
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"q": "b"}


def test_multipart_list_repeated_preserves_order():
    response = client.post(
        "/list", files=[("q", (None, "3")), ("q", (None, "1")), ("q", (None, "2"))]
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"q": [3, 1, 2]}


def test_multipart_strict_scalar_repeated_is_rejected():
    response = client.post(
        "/scalar-strict", files=[("q", (None, "a")), ("q", (None, "b"))]
    )
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["type"] == "multiple_values"
    assert error["loc"] == ["body", "q"]
