"""
Tests that the OpenAPI requestBody is aligned with the runtime multi-value
semantics of Form/File fields, including the strict option.
"""

from typing import Annotated

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.testclient import TestClient
from pydantic import BaseModel

app = FastAPI()


class SampleForm(BaseModel):
    name: str = "anonymous"
    age: int | None = None
    tags: list[str] = []


@app.post("/lenient")
def lenient_form(data: Annotated[SampleForm, Form()]):
    return data


@app.post("/strict")
def strict_form(data: Annotated[SampleForm, Form(strict=True)]):
    return data


@app.post("/sequences")
def sequences(
    list_field: Annotated[list[str], Form()],
    set_field: Annotated[set[str], Form()],
    tuple_field: Annotated[tuple[str, ...], Form()],
    optional_list: Annotated[list[str] | None, Form()] = None,
):
    return {}


@app.post("/files")
def files(
    files: Annotated[list[UploadFile], File()],
    optional_file: Annotated[UploadFile | None, File(strict=True)] = None,
):
    return {}


client = TestClient(app)


def get_request_body(path: str):
    spec = client.get("/openapi.json").json()
    return spec["paths"][path]["post"]["requestBody"], spec


def test_form_model_request_body_media_type():
    for path in ("/lenient", "/strict"):
        request_body, _ = get_request_body(path)
        assert list(request_body["content"]) == ["application/x-www-form-urlencoded"]
        assert request_body["required"] is True


def test_form_model_schema_aligned_with_strict_option():
    # The strict option only changes runtime parsing, the documented schema
    # is the same and aligned field by field
    _, spec = get_request_body("/strict")
    schema = spec["components"]["schemas"]["SampleForm"]
    assert schema["properties"]["name"] == {
        "type": "string",
        "title": "Name",
        "default": "anonymous",
    }
    assert schema["properties"]["age"] == {
        "anyOf": [{"type": "integer"}, {"type": "null"}],
        "title": "Age",
    }
    assert schema["properties"]["tags"] == {
        "type": "array",
        "items": {"type": "string"},
        "title": "Tags",
        "default": [],
    }


def test_sequence_fields_request_body():
    request_body, spec = get_request_body("/sequences")
    assert list(request_body["content"]) == ["application/x-www-form-urlencoded"]
    schema_name = request_body["content"]["application/x-www-form-urlencoded"][
        "schema"
    ]["$ref"].split("/")[-1]
    schema = spec["components"]["schemas"][schema_name]
    # list/set/tuple fields are all documented as arrays, aligned with the
    # runtime parsing that collects all the repeated values
    for field_name in ("list_field", "set_field", "tuple_field"):
        assert schema["properties"][field_name]["type"] == "array"
        assert schema["properties"][field_name]["items"] == {"type": "string"}
    assert schema["properties"]["optional_list"] == {
        "anyOf": [
            {"type": "array", "items": {"type": "string"}},
            {"type": "null"},
        ],
        "title": "Optional List",
    }
    assert schema["required"] == ["list_field", "set_field", "tuple_field"]


def test_file_fields_request_body():
    request_body, spec = get_request_body("/files")
    assert list(request_body["content"]) == ["multipart/form-data"]
    schema_name = request_body["content"]["multipart/form-data"]["schema"][
        "$ref"
    ].split("/")[-1]
    schema = spec["components"]["schemas"][schema_name]
    binary = {"type": "string", "contentMediaType": "application/octet-stream"}
    assert schema["properties"]["files"] == {
        "type": "array",
        "items": binary,
        "title": "Files",
    }
    assert schema["properties"]["optional_file"] == {
        "anyOf": [binary, {"type": "null"}],
        "title": "Optional File",
    }
    assert schema["required"] == ["files"]


def test_strict_and_lenient_request_bodies_are_identical():
    lenient_body, _ = get_request_body("/lenient")
    strict_body, _ = get_request_body("/strict")
    assert lenient_body == strict_body
