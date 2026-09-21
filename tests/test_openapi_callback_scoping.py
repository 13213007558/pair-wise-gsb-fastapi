import json
import os
import subprocess
import sys
import warnings

from pathlib import Path

import pytest

from fastapi import APIRouter, Depends, FastAPI
from fastapi.exceptions import FastAPIError
from fastapi.security import APIKeyHeader
from pydantic import BaseModel


class CallbackPayload(BaseModel):
    value: str


def test_callback_security_scheme_is_documented_in_components():
    callback_key = APIKeyHeader(name="X-Callback-Key", scheme_name="callback_key")
    main_key = APIKeyHeader(name="X-Main-Key", scheme_name="main_key")

    callback_router = APIRouter()

    @callback_router.post("{$callback_url}/events")
    def callback_event(body: CallbackPayload, api_key: str = Depends(callback_key)):
        pass  # pragma: no cover

    app = FastAPI()

    @app.post("/webhooks", callbacks=callback_router.routes)
    def register_webhook(api_key: str = Depends(main_key)):
        pass  # pragma: no cover

    schema = app.openapi()

    operation = schema["paths"]["/webhooks"]["post"]
    callback_operation = operation["callbacks"]["callback_event"][
        "{$callback_url}/events"
    ]["post"]

    assert operation["security"] == [{"main_key": []}]
    assert callback_operation["security"] == [{"callback_key": []}]
    schemes = schema["components"]["securitySchemes"]
    assert schemes["main_key"]["name"] == "X-Main-Key"
    assert schemes["callback_key"]["name"] == "X-Callback-Key"
    # The callback request body schema must be resolvable.
    assert (
        callback_operation["requestBody"]["content"]["application/json"]["schema"]
        == {"$ref": "#/components/schemas/CallbackPayload"}
    )
    assert "CallbackPayload" in schema["components"]["schemas"]


def test_callback_only_security_scheme_is_documented():
    callback_key = APIKeyHeader(name="X-Callback-Key", scheme_name="callback_key")

    callback_router = APIRouter()

    @callback_router.post("{$callback_url}/events")
    def callback_event(api_key: str = Depends(callback_key)):
        pass  # pragma: no cover

    app = FastAPI()

    @app.post("/webhooks", callbacks=callback_router.routes)
    def register_webhook():
        pass  # pragma: no cover

    schema = app.openapi()
    schemes = schema["components"]["securitySchemes"]
    assert schemes["callback_key"]["name"] == "X-Callback-Key"


def test_duplicate_callback_names_raise_locatable_error():
    callback_a = APIRouter()

    @callback_a.post("{$callback_url}/one")
    def shared_callback():
        pass  # pragma: no cover

    callback_b = APIRouter()

    @callback_b.post("{$callback_url}/two")
    def shared_callback():  # noqa: F811
        pass  # pragma: no cover

    app = FastAPI()

    @app.post(
        "/webhooks", callbacks=[*callback_a.routes, *callback_b.routes]
    )
    def register_webhook():
        pass  # pragma: no cover

    with pytest.raises(FastAPIError) as exc_info:
        app.openapi()
    message = str(exc_info.value)
    assert "Duplicate callback name 'shared_callback'" in message
    assert "/webhooks" in message
    assert "register_webhook" in message
    assert "{$callback_url}/two" in message


def test_duplicate_callback_names_from_included_router_raise_locatable_error():
    parent_callback = APIRouter()

    @parent_callback.post("{$callback_url}/parent")
    def shared_callback():
        pass  # pragma: no cover

    route_callback = APIRouter()

    @route_callback.post("{$callback_url}/route")
    def shared_callback():  # noqa: F811
        pass  # pragma: no cover

    inner_router = APIRouter()

    @inner_router.post("/events", callbacks=route_callback.routes)
    def create_event():
        pass  # pragma: no cover

    app = FastAPI()
    app.include_router(
        inner_router, prefix="/api", callbacks=parent_callback.routes
    )

    with pytest.raises(FastAPIError) as exc_info:
        app.openapi()
    message = str(exc_info.value)
    assert "Duplicate callback name 'shared_callback'" in message
    assert "/api/events" in message
    assert "create_event" in message


def test_callbacks_generated_once_for_multi_method_routes():
    callback_router = APIRouter()

    @callback_router.post("{$callback_url}/events/{$request.body.value}")
    def event_callback(body: CallbackPayload):
        pass  # pragma: no cover

    router = APIRouter()

    @router.api_route(
        "/webhooks", methods=["POST", "PUT"], callbacks=callback_router.routes
    )
    def register_webhook(body: CallbackPayload):
        pass  # pragma: no cover

    app = FastAPI()
    app.include_router(router)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        schema = app.openapi()

    callback_id_warnings = [
        str(w.message)
        for w in caught
        if "event_callback" in str(w.message) and "Operation ID" in str(w.message)
    ]
    assert callback_id_warnings == []
    for method in ("post", "put"):
        callbacks = schema["paths"]["/webhooks"][method]["callbacks"]
        path_item = callbacks["event_callback"][
            "{$callback_url}/events/{$request.body.value}"
        ]
        assert list(path_item) == ["post"]


def test_repeated_openapi_generation_is_stable():
    callback_router = APIRouter()

    @callback_router.post("{$callback_url}/events")
    def event_callback(body: CallbackPayload):
        pass  # pragma: no cover

    sub_router = APIRouter(prefix="/sub")

    @sub_router.post("/events", callbacks=callback_router.routes, operation_id="create")
    def create_event(body: CallbackPayload):
        pass  # pragma: no cover

    app = FastAPI(
        servers=[
            {
                "url": "https://{tenant}.example.com",
                "variables": {"tenant": {"default": "demo"}},
            }
        ]
    )
    app.include_router(sub_router, prefix="/api")

    @app.webhooks.post("wake")
    def wake_webhook(body: CallbackPayload):
        pass  # pragma: no cover

    first = json.dumps(app.openapi(), sort_keys=True)
    for _ in range(4):
        assert json.dumps(app.openapi(), sort_keys=True) == first


def test_callbacks_are_not_shared_mutably_between_methods():
    callback_router = APIRouter()

    @callback_router.post("{$callback_url}/events")
    def event_callback(body: CallbackPayload):
        pass  # pragma: no cover

    router = APIRouter()

    @router.api_route(
        "/webhooks",
        methods=["POST", "PUT"],
        callbacks=callback_router.routes,
        openapi_extra={"callbacks": {"extra": {"/extra": {"get": {}}}}},
    )
    def register_webhook(body: CallbackPayload):
        pass  # pragma: no cover

    app = FastAPI()
    app.include_router(router)

    # A single route with multiple methods shares one operationId; only the
    # callback/body isolation behavior is relevant for this test.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        schema = app.openapi()
    path_item = schema["paths"]["/webhooks"]
    post_callbacks = path_item["post"]["callbacks"]
    put_callbacks = path_item["put"]["callbacks"]
    assert post_callbacks == put_callbacks
    assert post_callbacks is not put_callbacks
    assert post_callbacks["event_callback"] is not put_callbacks["event_callback"]


def test_multi_method_operation_id_is_deterministic_across_hash_seeds():
    code = (
        "import sys; sys.path.insert(0, %r);"
        "from fastapi import FastAPI;"
        "app = FastAPI();"
        "app.api_route('/multi', methods=['POST', 'PUT'])(lambda: None);"
        "print(app.openapi()['paths']['/multi']['post']['operationId'])"
    ) % (str(Path.cwd()),)
    operation_ids = set()
    for seed in ("0", "1", "2", "7", "42"):
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
            check=True,
        )
        operation_ids.add(result.stdout.strip())
    assert operation_ids == {"_lambda__multi_post"}
