"""Shared authentication dependency used by both HTTP and WebSocket routes.

Three dependency variants compare exception styles on identical logic:

* ``require_principal_httpex``  -> raises ``HTTPException`` (HTTP-only type)
* ``require_principal_wsexc``   -> raises ``WebSocketException``
* ``require_principal_domain``  -> raises the domain ``AuthError``, mapped by
  scope-aware custom handlers (the recommended reusable pattern)

Every variant depends on ``managed_resource``, a real *yield* dependency that
opens a task group + background heartbeat.  FastAPI enters it while solving
dependencies and closes it through the per-route AsyncExitStack on every path,
so the probe can prove teardown always cancels the pending task.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from enum import Enum

import anyio
from fastapi import Depends

from .state import STATS


class FailureMode(str, Enum):
    OK = "ok"
    MISSING = "missing"          # no credentials -> 401 / 1008
    FORBIDDEN = "forbidden"      # authenticated, no permission -> 403 / 1008
    AUTH_DOWN = "auth_down"      # authentication service raises -> 5xx-class


class AuthError(Exception):
    """Transport-agnostic domain failure; one type for HTTP and WebSocket."""

    def __init__(self, kind: FailureMode, status_code: int, ws_code: int, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.status_code = status_code
        self.ws_code = ws_code
        self.detail = detail


class AuthServiceError(Exception):
    """The upstream authentication service itself failed (5xx-class)."""


SCENARIO = {
    FailureMode.MISSING: dict(status_code=401, ws_code=1008, detail="missing credentials"),
    FailureMode.FORBIDDEN: dict(status_code=403, ws_code=1008, detail="insufficient permission"),
    FailureMode.AUTH_DOWN: dict(status_code=503, ws_code=1011, detail="authentication service unavailable"),
}


def authenticate(mode: FailureMode) -> dict:
    """Pure authentication decision, shared by every transport/style."""
    if mode is FailureMode.OK:
        return {"sub": "user-42", "scope": "read"}
    spec = SCENARIO[mode]
    if mode is FailureMode.AUTH_DOWN:
        raise AuthServiceError(spec["detail"])
    raise AuthError(mode, spec["status_code"], spec["ws_code"], spec["detail"])


class _PooledResource:
    """Resource that owns its background task group.

    Entering/exiting the task group is the resource's responsibility, so the
    dependency generator's ``yield`` is not wrapped by a task group.  This is
    what allows an auth exception raised in the endpoint to keep its concrete
    type instead of being bundled into an ExceptionGroup.
    """

    def __init__(self) -> None:
        self.value = {"resource": "db-connection"}
        self.task_group = anyio.create_task_group()
        self.lifetime = anyio.CancelScope()

    async def __aenter__(self) -> dict:
        await self.task_group.__aenter__()

        async def background_heartbeat() -> None:
            STATS.bg_started += 1
            with self.lifetime:
                try:
                    await anyio.sleep_forever()
                except anyio.get_cancelled_exc_class():
                    STATS.bg_cancelled += 1
                    raise

        self.task_group.start_soon(background_heartbeat)
        return self.value

    async def __aexit__(self, *exc_info: object) -> None:
        self.lifetime.cancel()
        await self.task_group.__aexit__(*exc_info)


async def managed_resource() -> AsyncIterator[dict]:
    """Correct yield dependency: the task group is owned by the resource, not
    spanning the yield point.  FastAPI wraps this generator itself.
    """
    STATS.enter_count += 1
    resource = _PooledResource()
    value = await resource.__aenter__()
    try:
        yield value
    finally:
        await resource.__aexit__(None, None, None)
        STATS.exit_count += 1


async def managed_resource_spanning_yield() -> AsyncIterator[dict]:
    """ANTI-PATTERN kept for the matrix: a task group spans the yield point.

    Any non-cancellation exception raised while this is open is re-raised by
    AnyIO as an ExceptionGroup, so exception handlers registered for the
    original type never match and the failure surfaces as a 500.
    """
    STATS.enter_count += 1
    try:
        async with anyio.create_task_group() as task_group:

            async def background_heartbeat() -> None:
                STATS.bg_started += 1
                try:
                    while True:
                        await anyio.sleep_forever()
                except anyio.get_cancelled_exc_class():
                    STATS.bg_cancelled += 1
                    raise

            task_group.start_soon(background_heartbeat)
            try:
                yield {"resource": "db-connection", "task_group": task_group}
            except BaseException as exc:  # noqa: BLE001 - observe, then re-raise
                STATS.seen_exceptions.append(type(exc).__name__)
                STATS.last_exc_repr = repr(exc)
                raise
            finally:
                task_group.cancel_scope.cancel()
    finally:
        STATS.exit_count += 1


def _decide(mode: FailureMode, style: str) -> dict:
    try:
        return authenticate(mode)
    except AuthServiceError as exc:
        if style == "http":
            from starlette.exceptions import HTTPException

            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if style == "ws":
            from starlette.exceptions import WebSocketException

            raise WebSocketException(code=1011, reason=str(exc)) from exc
        raise AuthError(FailureMode.AUTH_DOWN, 503, 1011, str(exc)) from exc
    except AuthError as exc:
        if style == "http":
            from starlette.exceptions import HTTPException

            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        if style == "ws":
            from starlette.exceptions import WebSocketException

            raise WebSocketException(code=exc.ws_code, reason=exc.detail) from exc
        raise


async def require_principal_httpex(
    mode: FailureMode,
    resource: dict = Depends(managed_resource),
) -> dict:
    return _decide(mode, "http")


async def require_principal_wsexc(
    mode: FailureMode,
    resource: dict = Depends(managed_resource),
) -> dict:
    return _decide(mode, "ws")


async def require_principal_domain(
    mode: FailureMode,
    resource: dict = Depends(managed_resource),
) -> dict:
    return _decide(mode, "domain")
