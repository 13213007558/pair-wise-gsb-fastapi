from collections.abc import Sequence

from fastapi.security.base import SecurityBase


class SecurityRequirement:
    """
    Declare one OpenAPI security requirement: a group of security schemes
    that must **all** be satisfied together (logical AND) for a *path
    operation*.

    Pass a list of `SecurityRequirement` to the `security` parameter of a
    *path operation* (or *path operation decorator*) to describe alternative
    ways to authenticate: each `SecurityRequirement` in the list is one
    acceptable combination (logical OR between requirements).

    Schemes can be referenced by:

    * A security scheme instance (e.g. an `APIKeyHeader()` or
        `OAuth2PasswordBearer()` object). Its definition is registered in
        the OpenAPI `components.securitySchemes` automatically, under its
        `scheme_name`.
    * The name (`str`) of a security scheme defined elsewhere in the
        application, e.g. by a `Security()` dependency. The name must match
        a scheme present in the final OpenAPI `components.securitySchemes`,
        otherwise `get_openapi()` raises a `ValueError`.

    OAuth2 scopes belong to their own scheme, pass them with the `scopes`
    parameter, mapping each scheme name to its list of scopes.

    This is a documentation/contract declaration: it only shapes the
    generated OpenAPI `security` for the *path operation*, it does **not**
    enforce anything at runtime. Keep using `Security()`/`Depends()`
    dependencies (typically with the same scheme objects) to actually
    validate credentials, and declare the same combinations here so the
    OpenAPI document matches the runtime behavior.

    Example:

    ```python
    from fastapi import FastAPI, Security
    from fastapi.security import (
        APIKeyHeader,
        OAuth2PasswordBearer,
        SecurityRequirement,
    )

    api_key = APIKeyHeader(name="X-API-Key")
    oauth2 = OAuth2PasswordBearer(tokenUrl="token", scopes={"read": "Read"})

    app = FastAPI()


    @app.get(
        "/items",
        # Both credentials are required (AND inside the requirement)
        security=[SecurityRequirement(api_key, oauth2, scopes={"OAuth2PasswordBearer": ["read"]})],
    )
    def read_items(
        key: str = Security(api_key), token: str = Security(oauth2, scopes=["read"])
    ):
        return {"key": key}
    ```
    """

    def __init__(
        self,
        *schemes: SecurityBase | str,
        scopes: dict[str, Sequence[str]] | None = None,
    ) -> None:
        if not schemes:
            raise ValueError(
                "SecurityRequirement must declare at least one security "
                "scheme, an empty requirement group is not valid. To allow "
                "anonymous access to a *path operation*, pass `security=[]` "
                "to the *path operation* instead."
            )
        resolved_names: list[str] = []
        for scheme in schemes:
            if not isinstance(scheme, SecurityBase | str):
                raise TypeError(
                    "SecurityRequirement schemes must be security scheme "
                    "instances (e.g. APIKeyHeader(...)) or scheme name "
                    f"strings, got: {scheme!r}"
                )
            name = scheme if isinstance(scheme, str) else scheme.scheme_name
            if name in resolved_names:
                raise ValueError(
                    f"Duplicate security scheme {name!r} in "
                    "SecurityRequirement, each scheme can only appear once "
                    "per requirement group."
                )
            resolved_names.append(name)
        resolved_scopes = {
            name: list(scope_list) for name, scope_list in (scopes or {}).items()
        }
        unknown_scope_names = sorted(set(resolved_scopes) - set(resolved_names))
        if unknown_scope_names:
            raise ValueError(
                "OAuth2 scopes were declared for schemes not present in this "
                f"SecurityRequirement: {unknown_scope_names}. Scopes must "
                "belong to one of the declared schemes: "
                f"{resolved_names}."
            )
        self.schemes = schemes
        self.scheme_names = resolved_names
        self.scopes = resolved_scopes

    def __repr__(self) -> str:
        schemes = ", ".join(self.scheme_names)
        return f"SecurityRequirement({schemes})"
