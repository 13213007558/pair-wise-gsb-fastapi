from typing import Annotated, Any

import pytest
from fastapi import FastAPI, Security
from fastapi.security import (
    APIKeyCookie,
    APIKeyHeader,
    APIKeyQuery,
    AnonymousSecurity,
    OAuth2PasswordBearer,
    SecurityRequirement,
)
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException
from starlette.status import HTTP_403_FORBIDDEN
from starlette.websockets import WebSocketDisconnect

api_key_header = APIKeyHeader(name="x-api-key", auto_error=False, description="Header key")
api_key_query = APIKeyQuery(name="api_key", auto_error=False, description="Query key")
api_key_cookie = APIKeyCookie(name="api_key", auto_error=False, description="Cookie key")
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/token", auto_error=False, description="OAuth2 bearer"
)
anonymous = AnonymousSecurity()


def invalid_key(key: str | None) -> bool:
    return key is not None and key != "valid-key"


async def header_user(api_key: Annotated[str | None, Security(api_key_header)]):
    if invalid_key(api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")
    if api_key:
        return "header-user"
    return None


async def query_user(api_key: Annotated[str | None, Security(api_key_query)]):
    if invalid_key(api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")
    if api_key:
        return "query-user"
    return None


async def cookie_user(api_key: Annotated[str | None, Security(api_key_cookie)]):
    if invalid_key(api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")
    if api_key:
        return "cookie-user"
    return None


async def oauth_user(token: Annotated[str | None, Security(oauth2_scheme)]):
    if token == "expired":
        raise HTTPException(
            status_code=401,
            detail="Expired token",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )
    if token == "no-scope":
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN,
            detail="Not enough permissions",
            headers={"WWW-Authenticate": 'Bearer error="insufficient_scope", scope="read"'},
        )
    if token:
        return "oauth-user"
    return None


authentication = SecurityRequirement(
    Security(header_user),
    Security(query_user),
    Security(cookie_user),
    Security(oauth_user, scopes=["read"]),
    allow_anonymous=True,
)


@pytest.fixture()
def client():
    app = FastAPI()

    @app.post("/items")
    async def read_items(
        principal: Annotated[Any, Security(authentication)],
    ):
        return {"anonymous": principal is anonymous, "principal": principal}

    return TestClient(app)


def test_any_security_scheme_allows_header_query_cookie_and_bearer(client: TestClient):
    assert client.post("/items", headers={"x-api-key": "valid-key"}).json() == {
        "anonymous": False,
        "principal": "header-user",
    }
    assert client.post("/items?api_key=valid-key").json() == {
        "anonymous": False,
        "principal": "query-user",
    }
    assert client.post("/items", cookies={"api_key": "valid-key"}).json() == {
        "anonymous": False,
        "principal": "cookie-user",
    }
    assert client.post(
        "/items", headers={"Authorization": "Bearer valid-token"}
    ).json() == {"anonymous": False, "principal": "oauth-user"}


def test_missing_credentials_are_explicit_anonymous(client: TestClient):
    response = client.post("/items")
    assert response.status_code == 200
    assert response.json() == {"anonymous": True, "principal": anonymous}


def test_invalid_credentials_are_not_treated_as_anonymous(client: TestClient):
    response = client.post("/items", headers={"x-api-key": "bad"})
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid API key"}

    response = client.post("/items", headers={"Authorization": "Bearer expired"})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'


def test_scope_failure_returns_403_and_scope_challenge(client: TestClient):
    response = client.post("/items", headers={"Authorization": "Bearer no-scope"})
    assert response.status_code == 403
    assert response.headers["WWW-Authenticate"] == (
        'Bearer error="insufficient_scope", scope="read"'
    )


def test_dependency_overrides_apply_to_or_branches(client: TestClient):
    client.app.dependency_overrides[oauth_user] = lambda: "override-user"
    response = client.post("/items")
    assert response.status_code == 200
    assert response.json() == {"anonymous": False, "principal": "override-user"}


def test_openapi_uses_or_requirements_and_anonymous_option(client: TestClient):
    response = client.get("/openapi.json")
    operation = response.json()["paths"]["/items"]["post"]
    assert operation["security"] == [
        {"APIKeyHeader": []},
        {"APIKeyQuery": []},
        {"APIKeyCookie": []},
        {"OAuth2PasswordBearer": ["read"]},
        {},
    ]
    schemes = response.json()["components"]["securitySchemes"]
    assert schemes["APIKeyHeader"]["description"] == "Header key"
    assert schemes["OAuth2PasswordBearer"]["description"] == "OAuth2 bearer"


def test_required_or_group_aggregates_challenges_when_anonymous_is_disabled():
    app = FastAPI()
    required = SecurityRequirement(Security(header_user), Security(oauth_user))

    @app.get("/required")
    def required_route(principal: Annotated[Any, Security(required)]):
        return principal

    response = TestClient(app).get("/required")
    assert response.status_code == 401
    assert response.headers.get_list("WWW-Authenticate") == ["APIKey", "Bearer"]


def test_websocket_uses_or_security_without_consuming_body_more_than_once():
    app = FastAPI()
    ws_auth = SecurityRequirement(Security(query_user), allow_anonymous=True)

    @app.websocket("/ws")
    async def ws(websocket, principal: Annotated[Any, Security(ws_auth)]):
        await websocket.accept()
        await websocket.send_json({"principal": principal, "anonymous": principal is anonymous})

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            assert websocket.receive_json() == {"principal": anonymous, "anonymous": True}
        with client.websocket_connect("/ws?api_key=valid-key") as websocket:
            assert websocket.receive_json() == {
                "principal": "query-user",
                "anonymous": False,
            }
        with pytest.raises(WebSocketDisconnect) as exc_info:
            client.websocket_connect("/ws?api_key=bad")
        assert exc_info.value.code == 1008
