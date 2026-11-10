"""
Paths that BYPASS ``response_model`` filtering by contract - do not assume
every path auto-filters.

Per the documented contract (docs/en/docs/advanced/response-directly.md),
FastAPI passes these straight through without applying ``response_model``:

* returning a ``Response`` (or subclass such as ``JSONResponse``) directly
* returning a ``StreamingResponse``
* exception handlers (their responses never go through ``response_model``)

The ``/misuse/*`` endpoints below are the leaking misuse: they declare a
``response_model`` but hand FastAPI a pre-built response carrying internal
fields, so the password digest and admin note markers DO appear in the HTTP
body. The tests assert that leak explicitly to pin the contract.

The ``/safe/*`` endpoints are the correct integration for each path: when
you must build the response yourself, validate and serialize through the
public model first, then construct the response from the filtered data.
"""

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel

FAKE_PASSWORD_DIGEST = "FAKE-PASSWORD-DIGEST{sha256$9f3c2d7e}"
FAKE_ADMIN_NOTE = "FAKE-ADMIN-NOTE{internal-only-7d21}"
LEAK_MARKERS = (FAKE_PASSWORD_DIGEST, FAKE_ADMIN_NOTE)

PUBLIC_USER_FIELDS = {"username", "email"}


class PublicUser(BaseModel):
    username: str
    email: str


class UserRecord(BaseModel):
    username: str
    email: str
    password_digest: str
    admin_note: str


def make_user_record():
    return UserRecord(
        username="jdoe",
        email="jdoe@example.com",
        password_digest=FAKE_PASSWORD_DIGEST,
        admin_note=FAKE_ADMIN_NOTE,
    )


class InternalBoom(Exception):
    pass


class SafeBoom(Exception):
    pass


app = FastAPI()


# --- Misuse: leaking patterns (bypass response_model by contract) -------


@app.get("/misuse/json-response", response_model=PublicUser)
def misuse_json_response():
    # WRONG: response_model is silently ignored for Response subclasses.
    return JSONResponse(content=jsonable_encoder(make_user_record()))


@app.get("/misuse/raw-response", response_model=PublicUser)
def misuse_raw_response():
    # WRONG: a native Response is passed through untouched.
    return Response(
        content=make_user_record().model_dump_json(),
        media_type="application/json",
    )


@app.get("/misuse/stream", response_model=PublicUser)
def misuse_stream():
    # WRONG: streaming bodies are never filtered by response_model.
    def internal_stream():
        for username in ("jdoe", "asmith"):
            record = make_user_record()
            yield (
                record.model_copy(update={"username": username}).model_dump_json()
                + "\n"
            )

    return StreamingResponse(internal_stream(), media_type="application/x-ndjson")


@app.exception_handler(InternalBoom)
async def internal_boom_handler(request: Request, exc: InternalBoom):
    # WRONG: exception handler responses bypass response_model entirely.
    return JSONResponse(status_code=500, content=jsonable_encoder(make_user_record()))


@app.get("/misuse/exception", response_model=PublicUser)
def misuse_exception():
    raise InternalBoom()


# --- Safe: correct integration for each bypass path ---------------------


@app.get("/safe/json-response", response_model=PublicUser)
def safe_json_response():
    public = PublicUser.model_validate(make_user_record().model_dump())
    return JSONResponse(content=public.model_dump())


@app.get("/safe/raw-response", response_model=PublicUser)
def safe_raw_response():
    public = PublicUser.model_validate(make_user_record().model_dump())
    return Response(content=public.model_dump_json(), media_type="application/json")


@app.get("/safe/stream")
def safe_stream():
    def public_stream():
        for username in ("jdoe", "asmith"):
            public = PublicUser(username=username, email="jdoe@example.com")
            yield public.model_dump_json() + "\n"

    return StreamingResponse(public_stream(), media_type="application/x-ndjson")


@app.exception_handler(SafeBoom)
async def safe_boom_handler(request: Request, exc: SafeBoom):
    public = PublicUser.model_validate(make_user_record().model_dump())
    return JSONResponse(status_code=500, content=public.model_dump())


@app.get("/safe/exception")
def safe_exception():
    raise SafeBoom()


client = TestClient(app, raise_server_exceptions=False)


def assert_leaks(response):
    """Pin the documented bypass: internal markers reach the client."""
    for marker in LEAK_MARKERS:
        assert marker in response.text


def assert_filtered(response, status_code: int = 200):
    assert response.status_code == status_code, response.text
    for marker in LEAK_MARKERS:
        assert marker not in response.text


def test_json_response_bypasses_response_model_and_leaks():
    assert_leaks(client.get("/misuse/json-response"))


def test_raw_response_bypasses_response_model_and_leaks():
    assert_leaks(client.get("/misuse/raw-response"))


def test_streaming_response_bypasses_response_model_and_leaks():
    assert_leaks(client.get("/misuse/stream"))


def test_exception_handler_bypasses_response_model_and_leaks():
    response = client.get("/misuse/exception")
    assert response.status_code == 500, response.text
    assert_leaks(response)


def test_safe_json_response_is_filtered():
    response = client.get("/safe/json-response")
    assert_filtered(response)
    assert response.json() == {"username": "jdoe", "email": "jdoe@example.com"}
    assert set(response.json()) == PUBLIC_USER_FIELDS


def test_safe_raw_response_is_filtered():
    response = client.get("/safe/raw-response")
    assert_filtered(response)
    assert response.json() == {"username": "jdoe", "email": "jdoe@example.com"}
    assert set(response.json()) == PUBLIC_USER_FIELDS


def test_safe_stream_is_filtered():
    response = client.get("/safe/stream")
    assert_filtered(response)
    lines = [line for line in response.text.splitlines() if line]
    assert len(lines) == 2
    assert "password_digest" not in response.text


def test_safe_exception_handler_is_filtered():
    response = client.get("/safe/exception")
    assert_filtered(response, status_code=500)
    assert response.json() == {"username": "jdoe", "email": "jdoe@example.com"}
    assert set(response.json()) == PUBLIC_USER_FIELDS
