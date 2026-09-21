import threading
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated
from uuid import UUID

import pytest
from fastapi import BackgroundTasks, Depends, FastAPI, Header, SerializerContext
from fastapi.exceptions import ResponseValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, model_serializer


@dataclass(frozen=True)
class DataclassPayment:
    amount: Decimal
    external_id: UUID


class Payment(BaseModel):
    customer_name: str | None = Field(
        default=None, serialization_alias="customerAlias"
    )
    amount: Decimal
    external_id: UUID

    model_config = {"from_attributes": True}

    @model_serializer(mode="wrap")
    def serialize_payment(self, handler, info):
        data = handler(self)
        if info.context and info.context.get("customer_field"):
            source_name = "customerAlias" if info.by_alias else "customer_name"
            if source_name in data:
                data[info.context["customer_field"]] = data.pop(source_name)
        return data


class Envelope(BaseModel):
    payment: Payment | DataclassPayment | None
    items: list[Payment | DataclassPayment | None]


class ORMPayment:
    def __init__(self) -> None:
        self.customer_name = "ORM customer"
        self.amount = Decimal("1.50")
        self.external_id = UUID("11111111-1111-1111-1111-111111111111")


def tenant_serializer(
    serializer: SerializerContext,
    x_tenant: str = Header(default=""),
) -> SerializerContext:
    if x_tenant == "tenant-a":
        serializer.context["customer_field"] = "customerName"
        serializer.custom_encoders[Decimal] = str
        serializer.custom_encoders[UUID] = str
    elif x_tenant == "tenant-b":
        serializer.context["customer_field"] = "customer_name_b"
        serializer.custom_encoders[Decimal] = float
        serializer.custom_encoders[UUID] = lambda value: value.hex.upper()
    return serializer


app = FastAPI()


def envelope_payload() -> Envelope:
    dataclass_payment = DataclassPayment(
        amount=Decimal("2.25"),
        external_id=UUID("22222222-2222-2222-2222-222222222222"),
    )
    model_payment = Payment(
        customer_name="Pydantic customer",
        amount=Decimal("3.75"),
        external_id=UUID("33333333-3333-3333-3333-333333333333"),
    )
    return Envelope(
        payment=ORMPayment(),
        items=[model_payment, None, dataclass_payment],
    )


@app.get("/payments", response_model=Envelope)
def get_payments(
    serializer: Annotated[SerializerContext, Depends(tenant_serializer)],
) -> Envelope:
    return envelope_payload()


@app.get(
    "/payments/include",
    response_model=Envelope,
    response_model_include={"payment": {"amount", "external_id"}},
)
def get_payments_include(
    serializer: Annotated[SerializerContext, Depends(tenant_serializer)],
) -> Envelope:
    return envelope_payload()


@app.get(
    "/payments/exclude",
    response_model=Envelope,
    response_model_exclude={"payment": {"external_id"}},
)
def get_payments_exclude(
    serializer: Annotated[SerializerContext, Depends(tenant_serializer)],
) -> Envelope:
    return envelope_payload()


@app.get(
    "/payments/no-alias",
    response_model=Envelope,
    response_model_by_alias=False,
)
def get_payments_no_alias(
    serializer: Annotated[SerializerContext, Depends(tenant_serializer)],
) -> Envelope:
    return envelope_payload()


@app.get("/free-form")
async def get_free_form(serializer: SerializerContext) -> dict[str, object]:
    serializer.custom_encoders[Decimal] = str
    return {
        "missing": None,
        "amount": Decimal("4.50"),
        "items": [None, {"amount": Decimal("5.25")}],
    }


@app.get("/payments/context-only", response_model=Envelope)
async def get_payments_context_only(
    serializer: SerializerContext,
) -> Envelope:
    serializer.context["customer_field"] = "customerName"
    return envelope_payload()


@app.get("/custom-response")
def get_custom_response(serializer: SerializerContext) -> JSONResponse:
    serializer.custom_encoders[Decimal] = str
    return JSONResponse(
        serializer.encode(
            {
                "amount": Decimal("6.50"),
                "external_id": UUID("44444444-4444-4444-4444-444444444444"),
            }
        )
    )


@app.get("/streaming-response")
def get_streaming_response(serializer: SerializerContext) -> StreamingResponse:
    serializer.custom_encoders[Decimal] = str
    return StreamingResponse(iter([b"streamed"]), media_type="text/plain")


@app.get("/invalid-response", response_model=Envelope)
def get_invalid_response() -> dict[str, object]:
    return {
        "payment": None,
        "items": [{"amount": {"bad": "decimal"}, "external_id": "not-uuid"}],
    }


class Unserializable:
    pass


@app.get("/broad-encoder-none-bypass")
def get_broad_encoder_none_bypass(
    serializer: SerializerContext,
) -> dict[str, object]:
    serializer.custom_encoders[object] = lambda value: f"encoded:{value}"
    return {"missing": None, "present": Decimal("9.25")}


@app.get("/unserializable-custom-encoder-result")
def get_unserializable(serializer: SerializerContext) -> dict[str, object]:
    serializer.custom_encoders[Decimal] = lambda value: Unserializable()
    return {"amount": Decimal("7.50")}


isolation_started: dict[str, threading.Event] = {}
isolation_contexts: dict[str, int] = {}
isolation_gate = threading.Event()


@app.get("/isolation")
def get_isolation(
    x_request_id: Annotated[str, Header()],
    serializer: SerializerContext,
    background_tasks: BackgroundTasks,
) -> dict[str, object]:
    serializer.custom_encoders[Decimal] = str if x_request_id == "a" else float
    isolation_contexts[x_request_id] = id(serializer)

    def background_task() -> None:
        isolation_contexts[f"{x_request_id}-background"] = id(serializer)

    background_tasks.add_task(background_task)
    isolation_started[x_request_id].set()
    isolation_gate.wait(timeout=2)
    return {"amount": Decimal("8.50"), "request": x_request_id}


client = TestClient(app)


def test_tenant_json_bytes_nested_union_dataclass_orm_none() -> None:
    default_response = client.get("/payments")
    tenant_a_response = client.get(
        "/payments", headers={"x-tenant": "tenant-a"}
    )
    tenant_b_response = client.get(
        "/payments", headers={"x-tenant": "tenant-b"}
    )

    assert default_response.json()["payment"] == {
        "customerAlias": "ORM customer",
        "amount": "1.50",
        "external_id": "11111111-1111-1111-1111-111111111111",
    }
    assert tenant_a_response.json()["payment"] == {
        "customerName": "ORM customer",
        "amount": "1.50",
        "external_id": "11111111-1111-1111-1111-111111111111",
    }
    assert tenant_b_response.json()["payment"] == {
        "customer_name_b": "ORM customer",
        "amount": 1.5,
        "external_id": "11111111111111111111111111111111",
    }
    assert tenant_a_response.json()["items"][1] is None
    assert tenant_a_response.json()["items"][2] == {
        "amount": "2.25",
        "external_id": "22222222-2222-2222-2222-222222222222",
    }
    assert default_response.content != tenant_a_response.content
    assert default_response.content != tenant_b_response.content
    assert tenant_a_response.content != tenant_b_response.content


def test_context_does_not_change_openapi_schema() -> None:
    schema = client.get("/openapi.json").json()
    payment_schema = schema["components"]["schemas"]["Payment"]
    assert "customerAlias" in payment_schema["properties"]
    assert "customerName" not in payment_schema["properties"]
    assert "customer_name_b" not in payment_schema["properties"]
    assert payment_schema["properties"]["amount"]["type"] in {
        "number",
        "string",
    }
    assert payment_schema["properties"]["external_id"] == {
        "type": "string",
        "format": "uuid",
        "title": "External Id",
    }


def test_existing_include_exclude_and_by_alias_remain_authoritative() -> None:
    include_response = client.get(
        "/payments/include", headers={"x-tenant": "tenant-a"}
    )
    exclude_response = client.get(
        "/payments/exclude", headers={"x-tenant": "tenant-a"}
    )
    no_alias_response = client.get(
        "/payments/no-alias", headers={"x-tenant": "tenant-a"}
    )

    assert include_response.json()["payment"] == {
        "amount": "1.50",
        "external_id": "11111111-1111-1111-1111-111111111111",
    }
    assert "external_id" not in exclude_response.json()["payment"]
    assert no_alias_response.json()["payment"]["customerName"] == "ORM customer"
    assert "customerAlias" not in no_alias_response.json()["payment"]


def test_free_form_context_applies_to_nested_values_and_preserves_none() -> None:
    response = client.get("/free-form")
    assert response.content == (
        b'{"missing":null,"amount":"4.50","items":'
        b'[null,{"amount":"5.25"}]}'
    )


def test_context_only_still_uses_pydantic_context() -> None:
    response = client.get("/payments/context-only")
    assert response.json()["payment"]["customerName"] == "ORM customer"


def test_explicit_and_streaming_responses_bypass_response_serialization() -> None:
    assert client.get("/custom-response").content == (
        b'{"amount":"6.50","external_id":"44444444-4444-4444-4444-444444444444"}'
    )
    assert client.get("/streaming-response").content == b"streamed"


def test_response_validation_error_keeps_location_and_context() -> None:
    with pytest.raises(ResponseValidationError) as exc_info:
        client.get("/invalid-response")

    error = exc_info.value.errors()[0]
    assert error["loc"] == (
        "response",
        "items",
        0,
        "Payment",
        "amount",
    )
    assert error["type"] == "decimal_type"
    assert error["input"] == {"bad": "decimal"}


def test_unserializable_encoder_result_is_not_coerced_to_string() -> None:
    with pytest.raises(TypeError):
        client.get("/unserializable-custom-encoder-result")


def test_broad_custom_encoder_does_not_encode_none() -> None:
    response = client.get("/broad-encoder-none-bypass")
    assert response.json() == {"missing": None, "present": "encoded:9.25"}


def test_concurrent_requests_and_background_tasks_use_request_scoped_context() -> (
    None
):
    isolation_started["a"] = threading.Event()
    isolation_started["b"] = threading.Event()
    isolation_gate.clear()
    isolation_contexts.clear()

    responses: dict[str, object] = {}

    def request(request_id: str) -> None:
        responses[request_id] = client.get(
            "/isolation", headers={"x-request-id": request_id}
        )

    thread_a = threading.Thread(target=request, args=("a",))
    thread_b = threading.Thread(target=request, args=("b",))
    thread_a.start()
    thread_b.start()
    assert isolation_started["a"].wait(timeout=2)
    assert isolation_started["b"].wait(timeout=2)
    isolation_gate.set()
    thread_a.join(timeout=2)
    thread_b.join(timeout=2)

    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    response_a = responses["a"]
    response_b = responses["b"]
    assert response_a.json()["amount"] == "8.50"
    assert response_b.json()["amount"] == 8.5
    assert isolation_contexts["a"] != isolation_contexts["b"]
    assert isolation_contexts["a-background"] == isolation_contexts["a"]
    assert isolation_contexts["b-background"] != isolation_contexts["a"]
