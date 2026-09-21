import asyncio
import dataclasses
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from fastapi import (
    BackgroundTasks,
    FastAPI,
    Request,
    SerializerContext,
)
from fastapi.exceptions import ResponseValidationError
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.routing import APIRouter, serialize_response
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field

FIXED_UID = UUID("12345678-1234-5678-1234-567812345678")


class Money(BaseModel):
    amount: Decimal
    currency: str


class Item(BaseModel):
    name: str
    price: Decimal
    uid: UUID
    money: Money | None = None


class AliasedItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(alias="fullName")
    price: Decimal


@dataclasses.dataclass
class DataclassItem:
    name: str
    price: Decimal


class FakeORM:
    def __init__(self) -> None:
        self.name = "orm"
        self.price = Decimal("1.5")
        self.uid = FIXED_UID
        self.money = None
        self._sa_instance_state = object()


def make_item() -> Item:
    return Item(
        name="x",
        price=Decimal("3.5"),
        uid=FIXED_UID,
        money=Money(amount=Decimal("2.25"), currency="EUR"),
    )


tenant_string = SerializerContext(
    encoders={
        Decimal: lambda d: f"{d:.2f}",
        UUID: lambda u: u.hex,
    }
)
tenant_float = SerializerContext(encoders={Decimal: lambda d: float(d)})


def tenant_serializer(request: Request) -> SerializerContext | None:
    # The caller selects the encoder per request (here: per tenant) without
    # modifying any global state.
    return {
        "string": tenant_string,
        "float": tenant_float,
    }.get(request.headers.get("x-tenant"))


app = FastAPI()


@app.get("/item", response_model=Item, response_serializer=tenant_serializer)
def get_item() -> Item:
    return make_item()


@app.get("/async-item", response_model=Item, response_serializer=tenant_serializer)
async def get_async_item() -> Item:
    await asyncio.sleep(0.001)
    return make_item()


@app.get("/items", response_model=list[Item], response_serializer=tenant_string)
def get_items() -> list[Item]:
    return [make_item(), make_item()]


@app.get(
    "/union",
    response_model=Item | Money,
    response_serializer=tenant_string,
)
def get_union(kind: str = "item") -> Item | Money:
    if kind == "money":
        return Money(amount=Decimal("2.5"), currency="USD")
    return make_item()


@app.get("/maybe", response_model=Item | None, response_serializer=tenant_string)
def get_maybe(give: bool = False) -> Item | None:
    if give:
        return make_item()
    return None


@app.get(
    "/dataclass",
    response_model=DataclassItem,
    response_serializer=tenant_string,
)
def get_dataclass() -> DataclassItem:
    return DataclassItem(name="dc", price=Decimal("4.5"))


@app.get("/orm", response_model=Item, response_serializer=tenant_string)
def get_orm() -> FakeORM:
    return FakeORM()


@app.get("/raw", response_serializer=tenant_string)
def get_raw():
    return {"price": Decimal("3.5"), "uid": FIXED_UID}


@app.get(
    "/excluded",
    response_model=Item,
    response_model_exclude={"uid"},
    response_serializer=tenant_string,
)
def get_excluded() -> Item:
    return make_item()


@app.get(
    "/no-alias",
    response_model=AliasedItem,
    response_model_by_alias=False,
    response_serializer=tenant_string,
)
def get_no_alias() -> AliasedItem:
    return AliasedItem(name="aliased", price=Decimal("1.5"))


@app.get("/alias", response_model=AliasedItem, response_serializer=tenant_string)
def get_alias() -> AliasedItem:
    return AliasedItem(name="aliased", price=Decimal("1.5"))


@app.get("/stream", response_serializer=tenant_string)
def get_stream() -> StreamingResponse:
    return StreamingResponse(iter([b"raw-3.5"]), media_type="text/plain")


@app.get("/custom-response", response_serializer=tenant_string)
def get_custom_response() -> PlainTextResponse:
    return PlainTextResponse("plain-3.5")


@app.get("/stream-items", response_serializer=tenant_string)
def get_stream_items():
    yield {"price": Decimal("3.5")}


@app.get("/invalid", response_model=Item, response_serializer=tenant_string)
def get_invalid():
    return {"name": "bad", "price": "not-a-decimal", "uid": FIXED_UID}


@app.get("/unserializable", response_serializer=tenant_string)
def get_unserializable():
    return object()


background_calls: list[str] = []


def record_background(marker: str) -> None:
    background_calls.append(marker)


@app.get(
    "/with-background",
    response_model=Item,
    response_serializer=tenant_serializer,
)
def get_with_background(background_tasks: BackgroundTasks) -> Item:
    background_tasks.add_task(record_background, "done")
    return make_item()


router = APIRouter(response_serializer=tenant_string)


@router.get("/router-default", response_model=Item)
def get_router_default() -> Item:
    return make_item()


@router.get("/router-override", response_model=Item, response_serializer=tenant_float)
def get_router_override() -> Item:
    return make_item()


app.include_router(router)

client = TestClient(app)


def test_tenant_string_bytes() -> None:
    response = client.get("/item", headers={"x-tenant": "string"})
    assert response.status_code == 200, response.text
    assert response.content == (
        b'{"name":"x","price":"3.50",'
        b'"uid":"12345678123456781234567812345678",'
        b'"money":{"amount":"2.25","currency":"EUR"}}'
    )


def test_tenant_float_bytes() -> None:
    response = client.get("/item", headers={"x-tenant": "float"})
    assert response.status_code == 200, response.text
    assert response.content == (
        b'{"name":"x","price":3.5,'
        b'"uid":"12345678-1234-5678-1234-567812345678",'
        b'"money":{"amount":2.25,"currency":"EUR"}}'
    )


def test_no_serializer_uses_default_encoding() -> None:
    response = client.get("/item")
    assert response.status_code == 200, response.text
    # The default path is untouched: it uses Pydantic's dump_json fast
    # path, which serializes Decimal as a string.
    assert response.json() == {
        "name": "x",
        "price": "3.5",
        "uid": "12345678-1234-5678-1234-567812345678",
        "money": {"amount": "2.25", "currency": "EUR"},
    }


def test_nested_models_and_lists() -> None:
    response = client.get("/items")
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data) == 2
    for item in data:
        assert item["price"] == "3.50"
        assert item["money"]["amount"] == "2.25"


def test_union_branches() -> None:
    item_response = client.get("/union", params={"kind": "item"})
    assert item_response.status_code == 200, item_response.text
    assert item_response.json()["price"] == "3.50"
    money_response = client.get("/union", params={"kind": "money"})
    assert money_response.status_code == 200, money_response.text
    assert money_response.json() == {"amount": "2.50", "currency": "USD"}


def test_none_passes_through() -> None:
    response = client.get("/maybe")
    assert response.status_code == 200, response.text
    assert response.content == b"null"
    given = client.get("/maybe", params={"give": True})
    assert given.json()["price"] == "3.50"


def test_dataclass() -> None:
    response = client.get("/dataclass")
    assert response.status_code == 200, response.text
    assert response.json() == {"name": "dc", "price": "4.50"}


def test_orm_object() -> None:
    response = client.get("/orm")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "name": "orm",
        "price": "1.50",
        "uid": "12345678123456781234567812345678",
        "money": None,
    }


def test_no_response_model_with_context() -> None:
    response = client.get("/raw")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "price": "3.50",
        "uid": "12345678123456781234567812345678",
    }


def test_exclude_still_applies_with_context() -> None:
    response = client.get("/excluded")
    assert response.status_code == 200, response.text
    data = response.json()
    assert "uid" not in data
    assert data["price"] == "3.50"


def test_by_alias_still_applies_with_context() -> None:
    aliased = client.get("/alias")
    assert aliased.status_code == 200, aliased.text
    assert aliased.json() == {"fullName": "aliased", "price": "1.50"}
    no_alias = client.get("/no-alias")
    assert no_alias.status_code == 200, no_alias.text
    assert no_alias.json() == {"name": "aliased", "price": "1.50"}


def test_streaming_response_bypasses_context() -> None:
    response = client.get("/stream")
    assert response.status_code == 200, response.text
    assert response.content == b"raw-3.5"


def test_custom_response_bypasses_context() -> None:
    response = client.get("/custom-response")
    assert response.status_code == 200, response.text
    assert response.content == b"plain-3.5"


def test_streamed_items_bypass_context() -> None:
    response = client.get("/stream-items")
    assert response.status_code == 200, response.text
    assert response.content == b'{"price": 3.5}\n'


def test_response_validation_error_preserves_location() -> None:
    with pytest.raises(ResponseValidationError) as exc_info:
        client.get("/invalid")
    errors = exc_info.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"][0] == "response"
    assert errors[0]["loc"][-1] == "price"


@pytest.mark.anyio
async def test_unserializable_object_raises_instead_of_stringifying() -> None:
    with pytest.raises(ValueError):
        await serialize_response(
            field=None,
            response_content=object(),
            serializer_context=tenant_string,
        )


def test_background_task_does_not_share_encoder_state() -> None:
    background_calls.clear()
    response = client.get("/with-background", headers={"x-tenant": "string"})
    assert response.status_code == 200, response.text
    assert response.json()["price"] == "3.50"
    assert background_calls == ["done"]
    background_calls.clear()
    response = client.get("/with-background", headers={"x-tenant": "float"})
    assert response.json()["price"] == 3.5
    assert background_calls == ["done"]


def test_router_level_default_and_route_override() -> None:
    default = client.get("/router-default")
    assert default.status_code == 200, default.text
    assert default.json()["price"] == "3.50"
    override = client.get("/router-override")
    assert override.status_code == 200, override.text
    assert override.json()["price"] == 3.5


def test_openapi_schema_not_affected_by_serializer() -> None:
    bare_app = FastAPI()

    # Same endpoint name, so the generated summary/operationId match;
    # only the serializer differs.
    @bare_app.get("/item", response_model=Item)
    def get_item() -> Item:
        return make_item()

    with_serializer = client.get("/openapi.json").json()
    bare = TestClient(bare_app).get("/openapi.json").json()
    assert with_serializer["paths"]["/item"] == bare["paths"]["/item"]
    assert (
        with_serializer["components"]["schemas"]["Item"]
        == bare["components"]["schemas"]["Item"]
    )


def test_serializer_context_is_immutable() -> None:
    derived = tenant_float.with_encoders({UUID: lambda u: u.hex})
    assert UUID not in tenant_float.encoders
    assert UUID in derived.encoders
    assert derived.encoders[Decimal] is tenant_float.encoders[Decimal]


@pytest.mark.anyio
async def test_concurrent_requests_are_isolated() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as async_client:
        tenants = ["string", "float", "none"] * 20
        responses = await asyncio.gather(
            *[
                async_client.get("/async-item", headers={"x-tenant": tenant})
                for tenant in tenants
            ]
        )
    for tenant, response in zip(tenants, responses, strict=False):
        assert response.status_code == 200, response.text
        price = response.json()["price"]
        if tenant == "string":
            assert price == "3.50"
        elif tenant == "float":
            assert price == 3.5
        else:
            assert price == "3.5"
