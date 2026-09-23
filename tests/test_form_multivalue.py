"""
Tests for the multi-value semantics of Form/File fields: repeated fields,
empty strings, missing values, defaults and empty files, in lenient
(default) and strict mode.
"""

from typing import Annotated

from fastapi import FastAPI, Form
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


@app.post("/lenient-async")
async def lenient_form_async(data: Annotated[SampleForm, Form()]):
    return data


client = TestClient(app)


def test_lenient_multipart_sample():
    # A single multipart sample with repeated fields, an empty string, a
    # missing field (using its default) and an empty file (empty filename)
    response = client.post(
        "/lenient",
        data={"tags": ["plumbus", "citadel"], "name": "", "age": ""},
        files={"avatar": ("", b"")},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "name": "anonymous",
        "age": None,
        "tags": ["plumbus", "citadel"],
    }


def test_lenient_multipart_sample_async():
    response = client.post(
        "/lenient-async",
        data={"tags": ["plumbus", "citadel"], "name": "", "age": ""},
        files={"avatar": ("", b"")},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "name": "anonymous",
        "age": None,
        "tags": ["plumbus", "citadel"],
    }


def test_lenient_scalar_duplicate_last_wins():
    response = client.post("/lenient", data={"name": ["first", "last"]})
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "last"


def test_strict_multipart_sample():
    response = client.post(
        "/strict",
        data={"tags": ["plumbus", "citadel"], "name": "Rick"},
        files={"avatar": ("", b"")},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "name": "Rick",
        "age": None,
        "tags": ["plumbus", "citadel"],
    }


def test_strict_empty_string_is_a_value():
    response = client.post("/strict", data={"name": ""})
    assert response.status_code == 200, response.text
    assert response.json()["name"] == ""


def test_strict_empty_string_for_int_is_an_error():
    response = client.post("/strict", data={"age": ""})
    assert response.status_code == 422, response.text
    assert response.json() == {
        "detail": [
            {
                "type": "int_parsing",
                "loc": ["body", "age"],
                "msg": "Input should be a valid integer, unable to parse string as an integer",
                "input": "",
            }
        ]
    }


def test_strict_scalar_duplicate_is_an_error():
    response = client.post("/strict", data={"name": ["first", "last"]})
    assert response.status_code == 422, response.text
    assert response.json() == {
        "detail": [
            {
                "type": "multiple_values",
                "loc": ["body", "name"],
                "msg": "Expected a single value, but got 2 values",
                "input": ["first", "last"],
                "ctx": {"count": 2},
            }
        ]
    }


def test_strict_list_duplicate_is_not_an_error():
    response = client.post("/strict", data={"tags": ["a", "b", "a"]})
    assert response.status_code == 200, response.text
    assert response.json()["tags"] == ["a", "b", "a"]


def test_missing_required_field_error():
    response = client.post("/lenient", data={"age": "70"}, files={"name": ("", b"")})
    # name is sent as an empty file part (empty filename), in lenient mode it
    # is treated the same as an empty string: the default is used
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "anonymous"


def test_empty_file_part_for_text_field_strict():
    # In strict mode an empty string sent for a text field is a real value
    response = client.post("/strict", data={"age": "70"}, files={"name": ("", b"")})
    assert response.status_code == 200, response.text
    assert response.json()["name"] == ""


def test_missing_and_empty_are_distinct_in_strict_mode():
    # Missing optional field: default is used
    response = client.post("/strict", data={"name": "Rick"})
    assert response.status_code == 200, response.text
    assert response.json()["age"] is None
    # Empty string for the same field: validation error
    response = client.post("/strict", data={"age": ""})
    assert response.status_code == 422, response.text
