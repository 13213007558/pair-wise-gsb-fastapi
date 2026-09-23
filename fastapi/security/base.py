from collections.abc import Sequence
from typing import TypeVar

from fastapi.openapi.models import SecurityBase as SecurityBaseModel
from starlette.exceptions import HTTPException


class SecurityBase:
    model: SecurityBaseModel
    scheme_name: str


_MISSING_CREDENTIALS_ATTR = "__fastapi_missing_credentials__"

_HTTPExceptionT = TypeVar("_HTTPExceptionT", bound=HTTPException)


def _mark_missing_credentials(exc: _HTTPExceptionT) -> _HTTPExceptionT:
    """
    Flag an exception as caused by *missing* credentials (the client didn't
    provide any credentials for the scheme), as opposed to *invalid* credentials
    (the client provided credentials, but they were rejected).

    This is used by AnyOf to decide whether a request can fall back to
    anonymous access: anonymous fallback is only allowed when no credentials
    were provided at all, never when provided credentials were rejected.
    """
    setattr(exc, _MISSING_CREDENTIALS_ATTR, True)
    return exc


def _is_missing_credentials_error(exc: BaseException) -> bool:
    return getattr(exc, _MISSING_CREDENTIALS_ATTR, False) is True


class AnyOf:
    """
    Declarative group of security schemes with OR (any-of) semantics.

    Use it inside Security() to allow a request to be authenticated by **any
    one** of the given security schemes, instead of requiring all of them:

        from fastapi import FastAPI, Security
        from fastapi.security import AnyOf, APIKeyHeader, OAuth2PasswordBearer

        app = FastAPI()

        api_key = APIKeyHeader(name="X-API-Key")
        oauth2 = OAuth2PasswordBearer(tokenUrl="token")

        auth = AnyOf(api_key, oauth2, allow_anonymous=True)


        @app.get("/items")
        def read_items(credentials=Security(auth)):
            ...

    At runtime each scheme is tried in declaration order and the first one that
    authenticates the request wins. If none succeeds:

    * If any scheme rejected provided credentials (invalid credentials), the
      first such error is raised (e.g. a 401 for an expired token or a 403
      for insufficient scopes).
    * Otherwise (no credentials were provided), if allow_anonymous is True
      the dependency result is None (explicit anonymous access).
    * Otherwise a 401 "Not authenticated" error is raised, combining the
      WWW-Authenticate challenges of all the schemes.

    In the generated OpenAPI, the group is expressed as multiple security
    requirement objects (one per scheme, plus an empty object when
    allow_anonymous is enabled), which is the standard way to express OR
    semantics for security in OpenAPI.
    """

    def __init__(
        self,
        *schemes: "SecurityBase | AnyOf",
        allow_anonymous: bool = False,
    ):
        if not schemes:
            raise ValueError("AnyOf requires at least one security scheme")
        self.schemes: Sequence[SecurityBase | AnyOf] = list(schemes)
        self.allow_anonymous = allow_anonymous

    async def __call__(self) -> None:
        # AnyOf is a declarative group: it is never called directly, the
        # dependency solving engine handles its sub-schemes with OR semantics.
        raise RuntimeError(  # pragma: no cover
            "AnyOf is a declarative security group and cannot be called directly"
        )
