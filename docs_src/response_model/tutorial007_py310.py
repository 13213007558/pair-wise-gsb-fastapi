from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, SerializerContext
from pydantic import BaseModel, model_serializer

app = FastAPI()


def tenant_serializer(
    serializer: SerializerContext,
    x_tenant: Annotated[str | None, Header()] = None,
) -> SerializerContext:
    if x_tenant == "acme":
        serializer.context["customer_field"] = "customerName"
        serializer.custom_encoders[Decimal] = str
        serializer.custom_encoders[UUID] = str
    return serializer


class Customer(BaseModel):
    name: str
    balance: Decimal
    reference: UUID

    @model_serializer(mode="wrap")
    def serialize_customer(self, handler, info):
        data = handler(self)
        if info.context and info.context.get("customer_field"):
            data[info.context["customer_field"]] = data.pop("name")
        return data


@app.get("/customers/me", response_model=Customer)
def read_customer(
    serializer: Annotated[SerializerContext, Depends(tenant_serializer)],
) -> Customer:
    return Customer(
        name="Alice",
        balance=Decimal("100.25"),
        reference=UUID("11111111-1111-1111-1111-111111111111"),
    )
