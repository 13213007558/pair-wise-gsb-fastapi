from decimal import Decimal
from uuid import UUID

from fastapi import FastAPI, Request, SerializerContext
from pydantic import BaseModel


class Item(BaseModel):
    name: str
    price: Decimal
    uid: UUID


app = FastAPI()

# Pre-built, immutable serializer contexts, one per tenant encoding profile.
# They are never mutated and never stored in global request state.
tenant_legacy = SerializerContext(
    encoders={
        Decimal: lambda d: f"{d:.2f}",
        UUID: lambda u: u.hex,
    }
)
tenant_modern = SerializerContext(encoders={Decimal: lambda d: float(d)})

TENANT_SERIALIZERS = {
    "legacy": tenant_legacy,
    "modern": tenant_modern,
}


def select_tenant_serializer(request: Request) -> SerializerContext | None:
    # Called once per request. The returned context applies only to that
    # request's response. Returning None falls back to FastAPI's default
    # encoding. No global state is modified here.
    return TENANT_SERIALIZERS.get(request.headers.get("x-tenant"))


@app.get(
    "/items/{item_id}",
    response_model=Item,
    response_serializer=select_tenant_serializer,
)
def read_item(item_id: str) -> Item:
    return Item(
        name=item_id,
        price=Decimal("3.5"),
        uid=UUID("12345678-1234-5678-1234-567812345678"),
    )
