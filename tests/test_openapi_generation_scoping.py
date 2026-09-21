import json
import warnings

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.exceptions import FastAPIError
from fastapi.openapi.utils import get_openapi
from fastapi.security import APIKeyHeader
from fastapi.testclient import TestClient
from pydantic import BaseModel


class Item(BaseModel):
    name: str


def find_unresolvable_refs(schema: dict) -> list[str]:
    unresolvable: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/"):
                target = schema
                for part in ref[2:].split("/"):
                    part = part.replace("~1", "/").replace("~0", "~")
                    if isinstance(target, dict) and part in target:
                        target = target[part]
                    else:
                        unresolvable.append(ref)
                        break
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    return unresolvable


def build_callback_router(
    name: str = "notification", path: str = "{$request.body#/callback_url}"
) -> APIRouter:
    callback_router = APIRouter()

    @callback_router.post(path, name=name)
    def callback_handler(item: Item):  # pragma: no cover
        pass

    return callback_router


def test_duplicate_callback_key_raises_locatable_error():
    callback_router_a = build_callback_router(name="notification")
    callback_router_b = build_callback_router(
        name="notification", path="{$request.body#/other_url}"
    )
    app = FastAPI()

    @app.post(
        "/items",
        callbacks=[*callback_router_a.routes, *callback_router_b.routes],
    )
    def create_item(item: Item):  # pragma: no cover
        pass

    with pytest.raises(FastAPIError) as exc_info:
        get_openapi(title="t", version="1", routes=app.routes)
    message = str(exc_info.value)
    assert "'notification'" in message
    assert "POST /items" in message
    assert "callback_handler" in message
    assert __file__ in message


def test_same_callback_route_object_is_deduplicated():
    callback_router = build_callback_router()
    app = FastAPI()
    sub_router = APIRouter()

    @sub_router.post("/items", callbacks=callback_router.routes)
    def create_item(item: Item):  # pragma: no cover
        pass

    app.include_router(sub_router, callbacks=callback_router.routes)
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        schema = get_openapi(title="t", version="1", routes=app.routes)
    duplicate_warnings = [
        w for w in recorded if "Duplicate Operation ID" in str(w.message)
    ]
    assert duplicate_warnings == []
    callbacks = schema["paths"]["/items"]["post"]["callbacks"]
    assert list(callbacks.keys()) == ["notification"]
    assert list(callbacks["notification"].keys()) == ["{$request.body#/callback_url}"]


def test_callback_validation_error_schemas_are_resolvable():
    callback_router = build_callback_router()
    app = FastAPI()

    @app.get("/trigger", callbacks=callback_router.routes)
    def trigger():  # pragma: no cover
        pass

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        schema = get_openapi(title="t", version="1", routes=app.routes)
    schemas = schema["components"]["schemas"]
    assert "HTTPValidationError" in schemas
    assert "ValidationError" in schemas
    assert find_unresolvable_refs(schema) == []


def test_callback_security_scheme_is_resolvable():
    api_key = APIKeyHeader(name="x-key")
    callback_router = APIRouter()

    @callback_router.post("{$request.body#/callback_url}", name="notification")
    def callback_handler(key: str = Depends(api_key)):  # pragma: no cover
        pass

    app = FastAPI()

    @app.get("/trigger", callbacks=callback_router.routes)
    def trigger():  # pragma: no cover
        pass

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        schema = get_openapi(title="t", version="1", routes=app.routes)
    assert "APIKeyHeader" in schema["components"]["securitySchemes"]
    callback_operation = schema["paths"]["/trigger"]["get"]["callbacks"][
        "notification"
    ]["{$request.body#/callback_url}"]["post"]
    assert callback_operation["security"] == [{"APIKeyHeader": []}]
    assert find_unresolvable_refs(schema) == []


def build_complex_app() -> FastAPI:
    app = FastAPI(
        servers=[
            {"url": "/", "description": "Relative server"},
            {
                "url": "https://{tenant}.example.com/{version}",
                "variables": {
                    "tenant": {"default": "public"},
                    "version": {"default": "v1"},
                },
            },
        ]
    )
    callback_router = build_callback_router()
    sub_router = APIRouter()

    @sub_router.post(
        "/items", callbacks=callback_router.routes, operation_id="create_item_custom"
    )
    def create_item(item: Item):  # pragma: no cover
        pass

    nested_router = APIRouter(prefix="/v1")
    nested_router.include_router(sub_router, prefix="/shop")
    app.include_router(nested_router)

    @app.webhooks.post("/new-item", operation_id="new_item_webhook")
    def webhook_new_item(item: Item):  # pragma: no cover
        pass

    return app


def test_repeated_generation_is_stable_and_isolated():
    app = build_complex_app()
    schema_1 = app.openapi()
    schema_2 = app.openapi()
    assert schema_1 == schema_2

    fresh_1 = get_openapi(
        title=app.title,
        version=app.version,
        routes=app.routes,
        webhooks=app.webhooks.routes,
        servers=app.servers,
    )
    fresh_2 = get_openapi(
        title=app.title,
        version=app.version,
        routes=app.routes,
        webhooks=app.webhooks.routes,
        servers=app.servers,
    )
    assert json.dumps(fresh_1, sort_keys=True) == json.dumps(fresh_2, sort_keys=True)
    assert fresh_1 == schema_1

    # Mutating a generated schema must not pollute later generations
    operation = fresh_1["paths"]["/v1/shop/items"]["post"]
    operation["operationId"] = "polluted"
    fresh_1["components"]["schemas"]["Item"]["title"] = "Polluted"
    fresh_1["servers"][0]["url"] = "https://polluted.example.com"
    fresh_3 = get_openapi(
        title=app.title,
        version=app.version,
        routes=app.routes,
        webhooks=app.webhooks.routes,
        servers=app.servers,
    )
    assert fresh_3 == fresh_2
    assert fresh_3["paths"]["/v1/shop/items"]["post"]["operationId"] == (
        "create_item_custom"
    )
    assert fresh_3["components"]["schemas"]["Item"]["title"] == "Item"

    # References and expressions are stable across generations
    callback = fresh_3["paths"]["/v1/shop/items"]["post"]["callbacks"]["notification"]
    assert list(callback.keys()) == ["{$request.body#/callback_url}"]
    assert find_unresolvable_refs(fresh_3) == []
    assert find_unresolvable_refs(schema_1) == []
    assert fresh_3["webhooks"]["/new-item"]["post"]["operationId"] == (
        "new_item_webhook"
    )


def test_servers_per_generation_do_not_pollute_each_other():
    app = build_complex_app()
    tenant_a_servers = [{"url": "https://tenant-a.example.com"}]
    tenant_b_servers = [{"url": "/relative/base"}]
    schema_a = get_openapi(
        title="t", version="1", routes=app.routes, servers=tenant_a_servers
    )
    schema_b = get_openapi(
        title="t", version="1", routes=app.routes, servers=tenant_b_servers
    )
    assert schema_a["servers"] == [{"url": "https://tenant-a.example.com"}]
    assert schema_b["servers"] == [{"url": "/relative/base"}]
    assert tenant_a_servers == [{"url": "https://tenant-a.example.com"}]
    assert tenant_b_servers == [{"url": "/relative/base"}]
    # The app-level servers are not affected by per-call overrides
    assert app.servers[1]["variables"]["tenant"]["default"] == "public"
    schema_default = app.openapi()
    assert (
        schema_default["servers"][1]["url"] == "https://{tenant}.example.com/{version}"
    )


def test_root_path_servers_priority_and_no_accumulation():
    configured_servers = [
        {
            "url": "https://{tenant}.example.com",
            "variables": {"tenant": {"default": "public"}},
        }
    ]
    app = FastAPI(servers=configured_servers)

    @app.get("/items")
    def read_items():  # pragma: no cover
        return []

    proxied_client = TestClient(app, root_path="/tenant-a/api")
    proxied_schema = proxied_client.get("/openapi.json").json()
    assert [server["url"] for server in proxied_schema["servers"]] == [
        "/tenant-a/api",
        "https://{tenant}.example.com",
    ]
    # Server variables survive the proxy prefix handling
    assert proxied_schema["servers"][1]["variables"]["tenant"]["default"] == "public"

    other_client = TestClient(app, root_path="/tenant-b/api")
    other_schema = other_client.get("/openapi.json").json()
    assert other_schema["servers"][0]["url"] == "/tenant-b/api"

    clean_schema = TestClient(app).get("/openapi.json").json()
    assert [server["url"] for server in clean_schema["servers"]] == [
        "https://{tenant}.example.com"
    ]
    assert configured_servers == [
        {
            "url": "https://{tenant}.example.com",
            "variables": {"tenant": {"default": "public"}},
        }
    ]


@pytest.mark.parametrize("openapi_version", ["3.0.2", "3.1.0"])
def test_openapi_version_selection_is_stable(openapi_version: str):
    app = build_complex_app()
    schema_1 = get_openapi(
        title="t",
        version="1",
        openapi_version=openapi_version,
        routes=app.routes,
        webhooks=app.webhooks.routes,
        servers=app.servers,
    )
    schema_2 = get_openapi(
        title="t",
        version="1",
        openapi_version=openapi_version,
        routes=app.routes,
        webhooks=app.webhooks.routes,
        servers=app.servers,
    )
    assert schema_1["openapi"] == openapi_version
    assert json.dumps(schema_1, sort_keys=True) == json.dumps(schema_2, sort_keys=True)
    assert find_unresolvable_refs(schema_1) == []
