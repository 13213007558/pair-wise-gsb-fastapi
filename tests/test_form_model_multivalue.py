import json
from typing import Optional, Union

from fastapi import FastAPI, Form, UploadFile
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, Json

app = FastAPI()


class ProfileForm(BaseModel):
    name: str
    tags: list[int] = []
    nickname: Optional[str] = None
    avatar: Optional[UploadFile] = None
    attachments: list[UploadFile] = []


class SequencesForm(BaseModel):
    ints: list[int] = []
    unique: set[int] = set()
    pair: tuple[int, ...] = ()
    maybe: Optional[list[int]] = None


class Address(BaseModel):
    city: str
    zip_code: int


class NestedForm(BaseModel):
    address: Json[Address]
    items: list[int] = []


class AliasedForm(BaseModel):
    user_name: str = Field(alias="userName")
    tag_list: list[int] = Field(default=[], alias="tagList")


class UserForm(BaseModel):
    name: str
    email: str


class CompanyForm(BaseModel):
    company_name: str
    industry: str


@app.post("/profile")
async def post_profile(form: ProfileForm = Form()):
    return {
        "name": form.name,
        "tags": form.tags,
        "nickname": form.nickname,
        "avatar": form.avatar.filename if form.avatar is not None else None,
        "attachments": [item.filename for item in form.attachments],
    }


@app.post("/sequences")
async def post_sequences(form: SequencesForm = Form()):
    return {
        "ints": form.ints,
        "unique": sorted(form.unique),
        "pair": list(form.pair),
        "maybe": form.maybe,
    }


@app.post("/nested")
async def post_nested(form: NestedForm = Form()):
    return {"address": form.address.model_dump(), "items": form.items}


@app.post("/aliased-model")
async def post_aliased_model(form: AliasedForm = Form()):
    return {"user_name": form.user_name, "tag_list": form.tag_list}


@app.post("/form-union")
def post_form_union(data: Union[UserForm, CompanyForm] = Form()):
    return {"received": data}


client = TestClient(app)

URLENCODED = {"Content-Type": "application/x-www-form-urlencoded"}


def post_form(url: str, body: str):
    return client.post(url, content=body, headers=URLENCODED)


def test_multipart_sample_with_all_field_kinds():
    # A single multipart request combining repeated fields, an empty string,
    # a missing field that falls back to its default, and an empty file
    response = client.post(
        "/profile",
        files=[
            ("name", (None, "Ada")),
            ("tags", (None, "3")),
            ("tags", (None, "1")),
            ("nickname", (None, "")),
            ("attachments", ("", b"")),
            ("attachments", ("notes.txt", b"hello")),
        ],
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "name": "Ada",
        "tags": [3, 1],
        "nickname": None,
        "avatar": None,
        "attachments": ["notes.txt"],
    }


def test_urlencoded_model_with_repeated_and_empty_fields():
    response = post_form("/profile", "name=Ada&tags=3&tags=1&nickname=")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "name": "Ada",
        "tags": [3, 1],
        "nickname": None,
        "avatar": None,
        "attachments": [],
    }


def test_model_scalar_repeated_last_value_wins():
    response = post_form("/profile", "name=Grace&name=Ada")
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Ada"


def test_model_missing_required_field_error_location():
    response = post_form("/profile", "tags=1")
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["type"] == "missing"
    assert error["loc"] == ["body", "name"]


def test_model_list_set_tuple_and_optional_fields():
    response = post_form(
        "/sequences",
        "ints=3&ints=1&unique=2&unique=1&unique=2&pair=4&pair=5&maybe=7",
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "ints": [3, 1],
        "unique": [1, 2],
        "pair": [4, 5],
        "maybe": [7],
    }


def test_model_sequence_defaults_with_empty_strings():
    response = post_form("/sequences", "ints=&unique=&pair=&maybe=")
    assert response.status_code == 200, response.text
    assert response.json() == {"ints": [], "unique": [], "pair": [], "maybe": None}


def test_model_invalid_item_error_location():
    response = post_form("/sequences", "ints=1&ints=x")
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["type"] == "int_parsing"
    assert error["loc"] == ["body", "ints", 1]


def test_nested_model_from_json_form_field():
    body = "address=" + json.dumps({"city": "Paris", "zip_code": 75000})
    response = post_form("/nested", body + "&items=1&items=2")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "address": {"city": "Paris", "zip_code": 75000},
        "items": [1, 2],
    }


def test_nested_model_invalid_json_error_location():
    response = post_form("/nested", "address=not-json")
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["loc"] == ["body", "address"]


def test_nested_model_invalid_content_error_location():
    response = post_form("/nested", 'address={"city": "Paris", "zip_code": "x"}')
    assert response.status_code == 422, response.text
    error = response.json()["detail"][0]
    assert error["loc"][:2] == ["body", "address"]


def test_model_alias_extraction():
    response = post_form("/aliased-model", "userName=ada&tagList=1&tagList=2")
    assert response.status_code == 200, response.text
    assert response.json() == {"user_name": "ada", "tag_list": [1, 2]}


def test_model_alias_in_error_location():
    response = post_form("/aliased-model", "user_name=ada")
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["type"] == "missing"
    assert error["loc"] == ["body", "userName"]


def test_union_first_model():
    response = post_form("/form-union", "name=John&email=john@example.com")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "received": {"name": "John", "email": "john@example.com"}
    }


def test_union_second_model():
    response = post_form("/form-union", "company_name=Tech&industry=Software")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "received": {"company_name": "Tech", "industry": "Software"}
    }


def test_union_invalid_data_error_location():
    response = post_form("/form-union", "name=John&company_name=Tech")
    assert response.status_code == 422, response.text
    for error in response.json()["detail"]:
        assert error["loc"][0] == "body"


def test_union_empty_form():
    response = client.post("/form-union")
    assert response.status_code == 422, response.text


def test_openapi_form_model_with_files_uses_multipart():
    spec = client.get("/openapi.json").json()
    request_body = spec["paths"]["/profile"]["post"]["requestBody"]
    assert set(request_body["content"].keys()) == {"multipart/form-data"}
    schema = request_body["content"]["multipart/form-data"]["schema"]
    assert schema == {"$ref": "#/components/schemas/ProfileForm"}
    properties = spec["components"]["schemas"]["ProfileForm"]["properties"]
    assert properties["tags"]["type"] == "array"
    assert properties["tags"]["items"] == {"type": "integer"}
    assert properties["attachments"]["type"] == "array"
    assert properties["attachments"]["items"] == {
        "type": "string",
        "contentMediaType": "application/octet-stream",
    }
    assert "strict" not in json.dumps(request_body)


def test_openapi_form_model_without_files_uses_urlencoded():
    spec = client.get("/openapi.json").json()
    request_body = spec["paths"]["/sequences"]["post"]["requestBody"]
    assert set(request_body["content"].keys()) == {
        "application/x-www-form-urlencoded"
    }
    properties = spec["components"]["schemas"]["SequencesForm"]["properties"]
    assert properties["ints"]["type"] == "array"
    assert properties["ints"]["items"] == {"type": "integer"}
    assert properties["unique"]["type"] == "array"
    assert properties["unique"]["uniqueItems"] is True
    assert properties["pair"]["type"] == "array"
    maybe_types = {item.get("type") for item in properties["maybe"]["anyOf"]}
    assert maybe_types == {"array", "null"}


def test_openapi_union_request_body():
    spec = client.get("/openapi.json").json()
    request_body = spec["paths"]["/form-union"]["post"]["requestBody"]
    (media_type,) = request_body["content"].keys()
    assert media_type == "application/x-www-form-urlencoded"
    schema = request_body["content"][media_type]["schema"]
    assert schema["anyOf"] == [
        {"$ref": "#/components/schemas/UserForm"},
        {"$ref": "#/components/schemas/CompanyForm"},
    ]
