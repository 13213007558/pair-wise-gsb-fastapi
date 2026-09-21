from fastapi.testclient import TestClient

from docs_src.serializer_context.tutorial001 import app

client = TestClient(app)


def test_legacy_tenant_encoding() -> None:
    response = client.get("/items/portal-gun", headers={"x-tenant": "legacy"})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "name": "portal-gun",
        "price": "3.50",
        "uid": "12345678123456781234567812345678",
    }


def test_modern_tenant_encoding() -> None:
    response = client.get("/items/portal-gun", headers={"x-tenant": "modern"})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "name": "portal-gun",
        "price": 3.5,
        "uid": "12345678-1234-5678-1234-567812345678",
    }


def test_default_encoding_without_tenant() -> None:
    response = client.get("/items/portal-gun")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "name": "portal-gun",
        "price": "3.5",
        "uid": "12345678-1234-5678-1234-567812345678",
    }
