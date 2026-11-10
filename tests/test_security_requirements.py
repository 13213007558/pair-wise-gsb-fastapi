import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Security
from fastapi.security import (
    APIKeyHeader,
    OAuth2PasswordBearer,
    SecurityRequirement,
)
from fastapi.testclient import TestClient

api_key = APIKeyHeader(name="X-API-Key")
oauth2 = OAuth2PasswordBearer(tokenUrl="token", scopes={"read": "Read items"})

optional_api_key = APIKeyHeader(name="X-API-Key", auto_error=False)
optional_oauth2 = OAuth2PasswordBearer(
    tokenUrl="token", scopes={"read": "Read items"}, auto_error=False
)


def require_api_key(key: str = Security(api_key)) -> str:
    return key


def require_oauth2(token: str = Security(oauth2, scopes=["read"])) -> str:
    return token


def require_any_credential(
    key: str | None = Security(optional_api_key),
    token: str | None = Security(optional_oauth2),
) -> str:
    if key is None and token is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return key or token or ""


app = FastAPI()


@app.get(
    "/both",
    # Both credentials are required: AND inside the requirement group
    security=[
        SecurityRequirement(api_key, oauth2, scopes={"OAuth2PasswordBearer": ["read"]})
    ],
)
def read_both(
    key: str = Depends(require_api_key), token: str = Depends(require_oauth2)
):
    return {"key": key, "token": token}


@app.get(
    "/either",
    # One credential or the other: OR between requirement groups
    security=[
        SecurityRequirement(api_key),
        SecurityRequirement("OAuth2PasswordBearer"),
    ],
)
def read_either(credential: str = Depends(require_any_credential)):
    return {"credential": credential}


@app.get("/public", security=[])
def read_public():
    return {"msg": "anonymous allowed"}


client = TestClient(app)


def test_both_missing_api_key():
    response = client.get("/both", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 401, response.text


def test_both_missing_oauth_token():
    response = client.get("/both", headers={"X-API-Key": "secret"})
    assert response.status_code == 401, response.text


def test_both_missing_everything():
    response = client.get("/both")
    assert response.status_code == 401, response.text


def test_both_with_both_credentials():
    response = client.get(
        "/both", headers={"X-API-Key": "secret", "Authorization": "Bearer tok"}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"key": "secret", "token": "tok"}


def test_either_missing_both():
    response = client.get("/either")
    assert response.status_code == 401, response.text


def test_either_with_api_key_only():
    response = client.get("/either", headers={"X-API-Key": "secret"})
    assert response.status_code == 200, response.text
    assert response.json() == {"credential": "secret"}


def test_either_with_oauth_only():
    response = client.get("/either", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 200, response.text
    assert response.json() == {"credential": "tok"}


def test_public_anonymous():
    response = client.get("/public")
    assert response.status_code == 200, response.text
    assert response.json() == {"msg": "anonymous allowed"}


def test_openapi_security_combinations():
    schema = app.openapi()
    assert schema["paths"]["/both"]["get"]["security"] == [
        {"APIKeyHeader": [], "OAuth2PasswordBearer": ["read"]}
    ]
    assert schema["paths"]["/either"]["get"]["security"] == [
        {"APIKeyHeader": []},
        {"OAuth2PasswordBearer": []},
    ]
    assert schema["paths"]["/public"]["get"]["security"] == []
    assert set(schema["components"]["securitySchemes"]) == {
        "APIKeyHeader",
        "OAuth2PasswordBearer",
    }


def test_openapi_cache_does_not_keep_stale_combinations():
    first = app.openapi()
    assert first["paths"]["/public"]["get"]["security"] == []
    for route in app.routes:
        if getattr(route, "path", None) == "/public":
            route.security_requirements = [SecurityRequirement(api_key)]
    app.openapi_schema = None
    regenerated = app.openapi()
    assert regenerated["paths"]["/public"]["get"]["security"] == [{"APIKeyHeader": []}]
    # Restore for the other tests
    for route in app.routes:
        if getattr(route, "path", None) == "/public":
            route.security_requirements = []
    app.openapi_schema = None


def test_router_mounting_preserves_security_requirements():
    router_app = FastAPI()
    router = APIRouter()

    @router.get(
        "/item",
        security=[
            SecurityRequirement(
                api_key, oauth2, scopes={"OAuth2PasswordBearer": ["read"]}
            )
        ],
    )
    def read_item(
        key: str = Depends(require_api_key),
        token: str = Depends(require_oauth2),
    ):
        return {"key": key}

    router_app.include_router(router, prefix="/api")
    schema = router_app.openapi()
    assert schema["paths"]["/api/item"]["get"]["security"] == [
        {"APIKeyHeader": [], "OAuth2PasswordBearer": ["read"]}
    ]


def test_empty_requirement_group_is_rejected():
    with pytest.raises(ValueError, match="at least one security scheme"):
        SecurityRequirement()


def test_duplicate_scheme_is_rejected():
    with pytest.raises(ValueError, match="Duplicate security scheme"):
        SecurityRequirement(api_key, api_key)


def test_scopes_for_undeclared_scheme_are_rejected():
    with pytest.raises(ValueError, match="not present in this"):
        SecurityRequirement(api_key, scopes={"OAuth2PasswordBearer": ["read"]})


def test_reference_to_undefined_scheme_is_rejected():
    bad_app = FastAPI()

    @bad_app.get("/item", security=[SecurityRequirement("NoSuchScheme")])
    def read_item():
        return {}

    with pytest.raises(ValueError, match="NoSuchScheme"):
        bad_app.openapi()


def test_conflicting_scheme_definitions_are_rejected():
    conflict_app = FastAPI()
    other_api_key = APIKeyHeader(name="X-Other-Key", scheme_name="APIKeyHeader")

    @conflict_app.get(
        "/item",
        security=[SecurityRequirement(api_key), SecurityRequirement(other_api_key)],
    )
    def read_item():
        return {}

    with pytest.raises(ValueError, match="Conflicting definitions"):
        conflict_app.openapi()


def test_legacy_single_scheme_derivation_is_unchanged():
    legacy_app = FastAPI()

    @legacy_app.get("/one")
    def read_one(key: str = Security(api_key)):
        return {"key": key}

    @legacy_app.get("/optional")
    def read_optional(key: str | None = Security(optional_api_key)):
        return {"key": key}

    schema = legacy_app.openapi()
    assert schema["paths"]["/one"]["get"]["security"] == [{"APIKeyHeader": []}]
    # auto_error=False optional schemes keep their original OR meaning
    assert schema["paths"]["/optional"]["get"]["security"] == [{"APIKeyHeader": []}]
