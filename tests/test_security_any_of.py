import pytest
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Security,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.openapi.models import APIKey, APIKeyIn
from fastapi.security import (
    AnyOf,
    APIKeyCookie,
    APIKeyHeader,
    APIKeyQuery,
    OAuth2PasswordBearer,
    SecurityScopes,
)
from fastapi.security.base import SecurityBase
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection

api_key_header = APIKeyHeader(
    name="X-API-Key", scheme_name="APIKeyHeader", description="API key in header"
)
api_key_query = APIKeyQuery(name="api-key", scheme_name="APIKeyQuery")
api_key_cookie = APIKeyCookie(name="session", scheme_name="APIKeyCookie")
oauth2 = OAuth2PasswordBearer(
    tokenUrl="token",
    scheme_name="OAuth2PasswordBearer",
    description="OAuth2 bearer token",
    scopes={"read": "Read access", "write": "Write access"},
)

auth_or_anonymous = AnyOf(api_key_header, oauth2, allow_anonymous=True)
auth_required = AnyOf(api_key_header, oauth2)
auth_locations = AnyOf(api_key_header, api_key_query, api_key_cookie)

app = FastAPI()


@app.get("/or-anonymous")
def read_or_anonymous(credentials=Security(auth_or_anonymous)):
    return {"credentials": credentials}


@app.get("/or-required")
def read_or_required(credentials=Security(auth_required)):
    return {"credentials": credentials}


@app.get("/or-locations")
def read_or_locations(credentials=Security(auth_locations)):
    return {"credentials": credentials}


@app.get("/or-scopes")
def read_or_scopes(credentials=Security(auth_required, scopes=["read"])):
    return {"credentials": credentials}


def validate_token(credentials=Security(auth_or_anonymous)):
    if credentials == "expired-token":
        raise HTTPException(
            status_code=401,
            detail="Token expired",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )
    return credentials


@app.get("/or-validated")
def read_or_validated(credentials=Depends(validate_token)):
    return {"credentials": credentials}


def require_write_scope(
    security_scopes: SecurityScopes, credentials=Security(auth_required)
):
    if credentials is not None and "write" not in security_scopes.scopes:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return credentials


@app.get("/or-scope-checked")
def read_or_scope_checked(credentials=Depends(require_write_scope)):
    return {"credentials": credentials}


def nested_dependency(credentials=Security(auth_required)):
    return {"nested": credentials}


@app.get("/or-nested")
def read_or_nested(data=Depends(nested_dependency)):
    return data


@app.get("/or-declared-twice", dependencies=[Security(auth_required)])
def read_or_declared_twice(credentials=Security(auth_required)):
    return {"credentials": credentials}


class ConnectionKey(SecurityBase):
    def __init__(self):
        self.model = APIKey(**{"in": APIKeyIn.header}, name="X-API-Key")
        self.scheme_name = "ConnectionKey"

    async def __call__(self, conn: HTTPConnection):
        return conn.headers.get("X-API-Key")


ws_auth = AnyOf(ConnectionKey())


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, credentials=Security(ws_auth)):
    await websocket.accept()
    await websocket.send_json({"credentials": credentials})
    await websocket.close()


client = TestClient(app)


def test_or_api_key_header():
    response = client.get("/or-anonymous", headers={"X-API-Key": "secret"})
    assert response.status_code == 200, response.text
    assert response.json() == {"credentials": "secret"}


def test_or_oauth2_bearer():
    response = client.get("/or-anonymous", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 200, response.text
    assert response.json() == {"credentials": "tok"}


def test_or_anonymous_allowed():
    response = client.get("/or-anonymous")
    assert response.status_code == 200, response.text
    assert response.json() == {"credentials": None}


def test_or_required_no_credentials():
    response = client.get("/or-required")
    assert response.status_code == 401, response.text
    assert response.json() == {"detail": "Not authenticated"}
    # The challenges of all the schemes are combined
    assert response.headers["WWW-Authenticate"] == "APIKey, Bearer"


def test_or_required_api_key():
    response = client.get("/or-required", headers={"X-API-Key": "secret"})
    assert response.status_code == 200, response.text
    assert response.json() == {"credentials": "secret"}


def test_or_required_bearer():
    response = client.get("/or-required", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 200, response.text
    assert response.json() == {"credentials": "tok"}


def test_or_locations_header():
    response = client.get("/or-locations", headers={"X-API-Key": "h"})
    assert response.status_code == 200, response.text
    assert response.json() == {"credentials": "h"}


def test_or_locations_query():
    response = client.get("/or-locations", params={"api-key": "q"})
    assert response.status_code == 200, response.text
    assert response.json() == {"credentials": "q"}


def test_or_locations_cookie():
    client.cookies.set("session", "c")
    response = client.get("/or-locations")
    assert response.status_code == 200, response.text
    assert response.json() == {"credentials": "c"}
    client.cookies.clear()


def test_or_locations_no_credentials():
    response = client.get("/or-locations")
    assert response.status_code == 401, response.text
    assert response.json() == {"detail": "Not authenticated"}
    # All schemes use the same challenge, it is not duplicated
    assert response.headers["WWW-Authenticate"] == "APIKey"


def test_or_scopes_preserved_in_challenge():
    response = client.get("/or-scopes")
    assert response.status_code == 401, response.text
    assert response.json() == {"detail": "Not authenticated"}
    assert response.headers["WWW-Authenticate"] == "APIKey, Bearer"


def test_or_expired_token_no_anonymous_fallback():
    # Invalid (expired) credentials must not fall back to anonymous access
    response = client.get(
        "/or-validated", headers={"Authorization": "Bearer expired-token"}
    )
    assert response.status_code == 401, response.text
    assert response.json() == {"detail": "Token expired"}
    assert response.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'


def test_or_valid_token():
    response = client.get("/or-validated", headers={"Authorization": "Bearer valid"})
    assert response.status_code == 200, response.text
    assert response.json() == {"credentials": "valid"}


def test_or_scope_failure_is_403():
    response = client.get("/or-scope-checked", headers={"X-API-Key": "secret"})
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "Not enough permissions"}


def test_or_nested_dependency():
    response = client.get("/or-nested", headers={"X-API-Key": "secret"})
    assert response.status_code == 200, response.text
    assert response.json() == {"nested": "secret"}


def test_or_nested_dependency_no_credentials():
    response = client.get("/or-nested")
    assert response.status_code == 401, response.text
    assert response.headers["WWW-Authenticate"] == "APIKey, Bearer"


def test_or_declared_twice():
    response = client.get("/or-declared-twice", headers={"X-API-Key": "secret"})
    assert response.status_code == 200, response.text
    assert response.json() == {"credentials": "secret"}


def test_or_declared_twice_no_credentials():
    response = client.get("/or-declared-twice")
    assert response.status_code == 401, response.text
    assert response.headers["WWW-Authenticate"] == "APIKey, Bearer"


def test_or_websocket():
    with client.websocket_connect("/ws", headers={"X-API-Key": "secret"}) as ws:
        assert ws.receive_json() == {"credentials": "secret"}


def test_or_websocket_no_credentials():
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()


def test_openapi_security_is_or_not_and():
    response = client.get("/openapi.json")
    assert response.status_code == 200, response.text
    paths = response.json()["paths"]
    # Multiple security requirement objects (OR), not a single merged one (AND)
    assert paths["/or-anonymous"]["get"]["security"] == [
        {"APIKeyHeader": []},
        {"OAuth2PasswordBearer": []},
        {},
    ]
    assert paths["/or-required"]["get"]["security"] == [
        {"APIKeyHeader": []},
        {"OAuth2PasswordBearer": []},
    ]
    assert paths["/or-locations"]["get"]["security"] == [
        {"APIKeyHeader": []},
        {"APIKeyQuery": []},
        {"APIKeyCookie": []},
    ]
    assert paths["/or-scopes"]["get"]["security"] == [
        {"APIKeyHeader": ["read"]},
        {"OAuth2PasswordBearer": ["read"]},
    ]


def test_openapi_scheme_descriptions_preserved():
    response = client.get("/openapi.json")
    assert response.status_code == 200, response.text
    schemes = response.json()["components"]["securitySchemes"]
    assert schemes["APIKeyHeader"]["description"] == "API key in header"
    assert schemes["OAuth2PasswordBearer"]["description"] == "OAuth2 bearer token"


def test_any_of_requires_schemes():
    with pytest.raises(ValueError):
        AnyOf()


def test_dependency_override_of_alternative():
    override_app = FastAPI()
    override_app.dependency_overrides[api_key_header] = lambda: "overridden"

    @override_app.get("/items")
    def read_items(credentials=Security(auth_required)):
        return {"credentials": credentials}

    override_client = TestClient(override_app)
    response = override_client.get("/items")
    assert response.status_code == 200, response.text
    assert response.json() == {"credentials": "overridden"}


def test_dependency_override_of_whole_group():
    override_app = FastAPI()
    override_app.dependency_overrides[auth_required] = lambda: "group-overridden"

    @override_app.get("/items")
    def read_items(credentials=Security(auth_required)):
        return {"credentials": credentials}

    override_client = TestClient(override_app)
    response = override_client.get("/items")
    assert response.status_code == 200, response.text
    assert response.json() == {"credentials": "group-overridden"}


def test_auto_error_false_falls_through():
    optional_key = APIKeyHeader(name="X-Optional", scheme_name="K1", auto_error=False)
    required_key = APIKeyHeader(name="X-Required", scheme_name="K2")
    group = AnyOf(optional_key, required_key)
    fallthrough_app = FastAPI()

    @fallthrough_app.get("/items")
    def read_items(credentials=Security(group)):
        return {"credentials": credentials}

    fallthrough_client = TestClient(fallthrough_app)
    # The first scheme declines (auto_error=False), the second one authenticates
    response = fallthrough_client.get("/items", headers={"X-Required": "secret"})
    assert response.status_code == 200, response.text
    assert response.json() == {"credentials": "secret"}
    # None of the schemes authenticate
    response = fallthrough_client.get("/items")
    assert response.status_code == 401, response.text
    assert response.headers["WWW-Authenticate"] == "APIKey"


def test_scheme_called_once_for_multiple_declarations():
    calls = []

    class CountingKey(APIKeyHeader):
        async def __call__(self, request: Request):
            calls.append(1)
            return await super().__call__(request)

    counting_key = CountingKey(name="X-Counted", scheme_name="Counted")
    counting_group = AnyOf(counting_key)
    counting_app = FastAPI()

    @counting_app.get("/items", dependencies=[Security(counting_group)])
    def read_items(first=Security(counting_group), second=Security(counting_group)):
        return {"first": first, "second": second}

    counting_client = TestClient(counting_app)
    response = counting_client.get("/items", headers={"X-Counted": "secret"})
    assert response.status_code == 200, response.text
    assert response.json() == {"first": "secret", "second": "secret"}
    # The shared dependency cache solves the group (and the scheme) only once
    assert len(calls) == 1


def test_nested_any_of_group():
    inner = AnyOf(api_key_query, api_key_cookie)
    outer = AnyOf(api_key_header, inner)
    nested_app = FastAPI()

    @nested_app.get("/items")
    def read_items(credentials=Security(outer)):
        return {"credentials": credentials}

    nested_client = TestClient(nested_app)
    response = nested_client.get("/items", headers={"X-API-Key": "h"})
    assert response.status_code == 200, response.text
    assert response.json() == {"credentials": "h"}
    response = nested_client.get("/items", params={"api-key": "q"})
    assert response.status_code == 200, response.text
    assert response.json() == {"credentials": "q"}
    response = nested_client.get("/items")
    assert response.status_code == 401, response.text
    assert response.headers["WWW-Authenticate"] == "APIKey"
    # Nested groups are flattened in the OpenAPI security requirements
    response = nested_client.get("/openapi.json")
    assert response.status_code == 200, response.text
    security = response.json()["paths"]["/items"]["get"]["security"]
    assert security == [{"APIKeyHeader": []}, {"APIKeyQuery": []}, {"APIKeyCookie": []}]


def test_openapi_dedup_shared_schemes():
    shared_app = FastAPI()
    group_one = AnyOf(api_key_header, oauth2)
    group_two = AnyOf(api_key_header, api_key_query)

    @shared_app.get("/one")
    def read_one(credentials=Security(group_one)):
        return {"credentials": credentials}

    @shared_app.get("/two")
    def read_two(credentials=Security(group_two)):
        return {"credentials": credentials}

    shared_client = TestClient(shared_app)
    response = shared_client.get("/openapi.json")
    assert response.status_code == 200, response.text
    paths = response.json()["paths"]
    assert paths["/one"]["get"]["security"] == [
        {"APIKeyHeader": []},
        {"OAuth2PasswordBearer": []},
    ]
    assert paths["/two"]["get"]["security"] == [
        {"APIKeyHeader": []},
        {"APIKeyQuery": []},
    ]
    # Each scheme is registered only once in the components
    schemes = response.json()["components"]["securitySchemes"]
    assert list(schemes.keys()) == [
        "APIKeyHeader",
        "OAuth2PasswordBearer",
        "APIKeyQuery",
    ]


def test_default_behavior_unchanged():
    # Multiple plain Security() dependencies keep the legacy AND runtime
    # semantics and the legacy OpenAPI output
    legacy_app = FastAPI()

    @legacy_app.get("/items")
    def read_items(
        key=Security(api_key_header), token=Security(oauth2)
    ):  # pragma: no cover
        return {"key": key, "token": token}

    legacy_client = TestClient(legacy_app)
    response = legacy_client.get("/items", headers={"X-API-Key": "secret"})
    assert response.status_code == 401, response.text
    response = legacy_client.get(
        "/items", headers={"X-API-Key": "secret", "Authorization": "Bearer tok"}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"key": "secret", "token": "tok"}
    response = legacy_client.get("/openapi.json")
    assert response.status_code == 200, response.text
    security = response.json()["paths"]["/items"]["get"]["security"]
    assert security == [{"APIKeyHeader": []}, {"OAuth2PasswordBearer": []}]
