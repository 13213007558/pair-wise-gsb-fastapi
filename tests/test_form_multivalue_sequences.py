"""
Tests for multi-value semantics of sequence (list/set/tuple) and Optional
Form/File fields, file lists and empty files.
"""

from typing import Annotated

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.testclient import TestClient

app = FastAPI()


@app.post("/sequences")
def sequences(
    list_field: Annotated[list[str], Form()],
    set_field: Annotated[set[str], Form()],
    tuple_field: Annotated[tuple[str, ...], Form()],
    optional_list: Annotated[list[str] | None, Form()] = None,
):
    return {
        "list_field": list_field,
        "set_field": sorted(set_field),
        "tuple_field": list(tuple_field),
        "optional_list": optional_list,
    }


@app.post("/sequences-strict")
def sequences_strict(
    list_field: Annotated[list[str], Form(strict=True)],
    optional_list: Annotated[list[str] | None, Form(strict=True)] = None,
):
    return {"list_field": list_field, "optional_list": optional_list}


@app.post("/files")
def files(
    files: Annotated[list[UploadFile], File()],
    optional_file: Annotated[UploadFile | None, File()] = None,
):
    return {
        "files": [{"filename": f.filename, "size": f.size} for f in files],
        "optional_file": optional_file.filename if optional_file else None,
    }


@app.post("/files-strict")
def files_strict(
    file: Annotated[UploadFile, File(strict=True)],
):
    return {"filename": file.filename, "size": file.size}


@app.post("/bytes-files")
async def bytes_files(
    files: Annotated[list[bytes], File()],
):
    return {"contents": [content.decode() for content in files]}


client = TestClient(app)


def test_sequence_fields_multi_values():
    response = client.post(
        "/sequences",
        data={
            "list_field": ["a", "b", "a"],
            "set_field": ["x", "y", "x"],
            "tuple_field": ["1", "2"],
            "optional_list": ["p", "q"],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "list_field": ["a", "b", "a"],
        "set_field": ["x", "y"],
        "tuple_field": ["1", "2"],
        "optional_list": ["p", "q"],
    }


def test_sequence_field_order_preserved():
    response = client.post(
        "/sequences",
        data={
            "list_field": ["z", "a", "m"],
            "set_field": ["b"],
            "tuple_field": ["3", "1", "2"],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["list_field"] == ["z", "a", "m"]
    assert response.json()["tuple_field"] == ["3", "1", "2"]


def test_sequence_fields_missing():
    response = client.post("/sequences", data={})
    assert response.status_code == 422, response.text
    assert response.json() == {
        "detail": [
            {
                "type": "missing",
                "loc": ["body", "list_field"],
                "msg": "Field required",
                "input": None,
            },
            {
                "type": "missing",
                "loc": ["body", "set_field"],
                "msg": "Field required",
                "input": None,
            },
            {
                "type": "missing",
                "loc": ["body", "tuple_field"],
                "msg": "Field required",
                "input": None,
            },
        ]
    }


def test_optional_list_missing_and_single():
    response = client.post(
        "/sequences",
        data={"list_field": ["a"], "set_field": ["x"], "tuple_field": ["1"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["optional_list"] is None
    response = client.post(
        "/sequences",
        data={
            "list_field": ["a"],
            "set_field": ["x"],
            "tuple_field": ["1"],
            "optional_list": "single",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["optional_list"] == ["single"]


def test_sequences_strict_allows_multiple_values():
    response = client.post(
        "/sequences-strict",
        data={"list_field": ["a", "b"], "optional_list": ["x", "y"]},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"list_field": ["a", "b"], "optional_list": ["x", "y"]}


def test_file_list_with_empty_file():
    response = client.post(
        "/files",
        files=[
            ("files", ("a.txt", b"aaa")),
            ("files", ("", b"")),
            ("files", ("b.txt", b"b")),
        ],
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "files": [
            {"filename": "a.txt", "size": 3},
            {"filename": "b.txt", "size": 1},
        ],
        "optional_file": None,
    }


def test_file_list_order_preserved():
    response = client.post(
        "/files",
        files=[
            ("files", ("z.txt", b"z")),
            ("files", ("a.txt", b"a")),
            ("files", ("m.txt", b"m")),
        ],
    )
    assert response.status_code == 200, response.text
    assert [f["filename"] for f in response.json()["files"]] == [
        "z.txt",
        "a.txt",
        "m.txt",
    ]


def test_only_empty_files_is_missing():
    response = client.post("/files", files={"files": ("", b"")})
    assert response.status_code == 422, response.text
    assert response.json() == {
        "detail": [
            {
                "type": "missing",
                "loc": ["body", "files"],
                "msg": "Field required",
                "input": None,
            }
        ]
    }


def test_optional_file_with_empty_file():
    response = client.post(
        "/files",
        files=[("files", ("a.txt", b"aaa")), ("optional_file", ("", b""))],
    )
    assert response.status_code == 200, response.text
    assert response.json()["optional_file"] is None


def test_strict_file_duplicate_is_an_error():
    response = client.post(
        "/files-strict",
        files=[("file", ("a.txt", b"a")), ("file", ("b.txt", b"b"))],
    )
    assert response.status_code == 422, response.text
    assert response.json() == {
        "detail": [
            {
                "type": "multiple_values",
                "loc": ["body", "file"],
                "msg": "Expected a single value, but got 2 values",
                "input": ["a.txt", "b.txt"],
                "ctx": {"count": 2},
            }
        ]
    }


def test_strict_empty_file_is_missing():
    response = client.post("/files-strict", files={"file": ("", b"")})
    assert response.status_code == 422, response.text
    assert response.json() == {
        "detail": [
            {
                "type": "missing",
                "loc": ["body", "file"],
                "msg": "Field required",
                "input": None,
            }
        ]
    }


def test_bytes_file_list_with_empty_file():
    response = client.post(
        "/bytes-files",
        files=[
            ("files", ("a.txt", b"foo")),
            ("files", ("", b"")),
            ("files", ("b.txt", b"bar")),
        ],
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"contents": ["foo", "bar"]}
