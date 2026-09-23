from typing import Optional

from fastapi import Depends, FastAPI, File, Form, UploadFile
from fastapi.testclient import TestClient

app = FastAPI()


@app.post("/file")
async def post_file(f: UploadFile = File()):
    return {"filename": f.filename, "size": len(await f.read())}


@app.post("/file-optional")
async def post_file_optional(f: Optional[UploadFile] = File(None)):
    if f is None:
        return {"f": None}
    return {"filename": f.filename, "size": len(await f.read())}


@app.post("/file-strict")
async def post_file_strict(f: UploadFile = File(strict=True)):
    return {"filename": f.filename, "size": len(await f.read())}


@app.post("/files")
async def post_files(f: list[UploadFile] = File()):
    return [
        {"filename": item.filename, "size": len(await item.read())} for item in f
    ]


@app.post("/files-optional")
async def post_files_optional(f: Optional[list[UploadFile]] = File(None)):
    if f is None:
        return {"f": None}
    return [{"filename": item.filename} for item in f]


@app.post("/bytes")
async def post_bytes(f: bytes = File()):
    return {"size": len(f)}


@app.post("/bytes-list")
async def post_bytes_list(f: list[bytes] = File()):
    return {"sizes": [len(item) for item in f]}


dependency_calls: list[str] = []


def form_dependency(name: str = Form()) -> str:
    dependency_calls.append(name)
    return name


@app.post("/dependency-cache-async")
async def post_dependency_cache_async(
    a: str = Depends(form_dependency), b: str = Depends(form_dependency)
):
    return {"a": a, "b": b, "calls": len(dependency_calls)}


@app.post("/dependency-cache-sync")
def post_dependency_cache_sync(
    a: str = Depends(form_dependency), b: str = Depends(form_dependency)
):
    return {"a": a, "b": b, "calls": len(dependency_calls)}


@app.post("/sync")
def post_sync(q: list[int] = Form(), name: Optional[str] = Form(None)):
    return {"q": q, "name": name}


@app.post("/async")
async def post_async(q: list[int] = Form(), name: Optional[str] = Form(None)):
    return {"q": q, "name": name}


client = TestClient(app)

EMPTY_FILE = ("", b"")


def test_empty_file_is_missing_for_required_file():
    response = client.post("/file", files={"f": EMPTY_FILE})
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["type"] == "missing"
    assert error["loc"] == ["body", "f"]


def test_real_file():
    response = client.post("/file", files={"f": ("a.txt", b"hello")})
    assert response.status_code == 200, response.text
    assert response.json() == {"filename": "a.txt", "size": 5}


def test_empty_file_is_default_for_optional_file():
    response = client.post("/file-optional", files={"f": EMPTY_FILE})
    assert response.status_code == 200, response.text
    assert response.json() == {"f": None}


def test_missing_optional_file():
    response = client.post("/file-optional", data={"other": "1"})
    assert response.status_code == 200, response.text
    assert response.json() == {"f": None}


def test_empty_files_are_skipped_in_file_lists():
    response = client.post(
        "/files",
        files=[("f", EMPTY_FILE), ("f", ("a.txt", b"hi")), ("f", EMPTY_FILE)],
    )
    assert response.status_code == 200, response.text
    assert response.json() == [{"filename": "a.txt", "size": 2}]


def test_only_empty_files_is_missing_for_required_file_list():
    response = client.post("/files", files=[("f", EMPTY_FILE)])
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["type"] == "missing"
    assert error["loc"] == ["body", "f"]


def test_file_list_preserves_order():
    response = client.post(
        "/files",
        files=[
            ("f", ("c.txt", b"ccc")),
            ("f", ("a.txt", b"a")),
            ("f", ("b.txt", b"bb")),
        ],
    )
    assert response.status_code == 200, response.text
    assert response.json() == [
        {"filename": "c.txt", "size": 3},
        {"filename": "a.txt", "size": 1},
        {"filename": "b.txt", "size": 2},
    ]


def test_optional_file_list_with_only_empty_file():
    response = client.post("/files-optional", files=[("f", EMPTY_FILE)])
    assert response.status_code == 200, response.text
    assert response.json() == {"f": None}


def test_strict_file_accepts_single_file():
    response = client.post("/file-strict", files={"f": ("a.txt", b"hi")})
    assert response.status_code == 200, response.text
    assert response.json() == {"filename": "a.txt", "size": 2}


def test_strict_file_rejects_multiple_files():
    response = client.post(
        "/file-strict",
        files=[("f", ("a.txt", b"a")), ("f", ("b.txt", b"b"))],
    )
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["type"] == "multiple_values"
    assert error["loc"] == ["body", "f"]
    assert error["msg"] == "Expected a single value, got 2 values"


def test_strict_file_ignores_empty_file_parts():
    response = client.post(
        "/file-strict", files=[("f", EMPTY_FILE), ("f", ("a.txt", b"hi"))]
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"filename": "a.txt", "size": 2}


def test_bytes_file():
    response = client.post("/bytes", files={"f": ("a.bin", b"12345")})
    assert response.status_code == 200, response.text
    assert response.json() == {"size": 5}


def test_bytes_list_skips_empty_files_and_preserves_order():
    response = client.post(
        "/bytes-list",
        files=[("f", EMPTY_FILE), ("f", ("a.bin", b"12")), ("f", ("b.bin", b"3"))],
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"sizes": [2, 1]}


def test_dependency_cache_async_endpoint():
    dependency_calls.clear()
    response = client.post(
        "/dependency-cache-async",
        content="name=ada",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"a": "ada", "b": "ada", "calls": 1}


def test_dependency_cache_sync_endpoint():
    dependency_calls.clear()
    response = client.post(
        "/dependency-cache-sync",
        content="name=ada",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"a": "ada", "b": "ada", "calls": 1}


def test_sync_and_async_endpoints_parse_form_the_same_way():
    body = "q=3&q=&q=1"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    sync_response = client.post("/sync", content=body, headers=headers)
    async_response = client.post("/async", content=body, headers=headers)
    assert sync_response.status_code == 200, sync_response.text
    assert async_response.status_code == 200, async_response.text
    assert sync_response.json() == async_response.json() == {"q": [3, 1], "name": None}
