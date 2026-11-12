"""Recommended, reusable auth boundary for shared HTTP + WebSocket dependencies.

Design rules (each verified in tests/test_boundary.py):

1. The dependency raises ONE transport-agnostic domain exception (``AuthError``).
   It never imports HTTPException/WebSocketException, so the same dependency is
   safe to reuse on both route kinds.
2. A single registered handler dispatches on the connection type:
   http -> JSONResponse with the mapped status code;
   ws   -> websocket.close(code, reason), legal in every state and on every
           compliant server (it never needs the denial-response extension).
3. Yield resources are entered before the auth decision and always exit via
   FastAPI's per-route AsyncExitStack; failures cancel pending tasks.
4. Auth failures are never downgraded into an accepted socket + business message.
5. Optional ASGI middleware makes denial-response usage spec-safe on servers that
   do not advertise ``websocket.http.response`` (framework gap, see README).
"""

from __future__ import annotations

from dataclasses import dataclass

import anyio
from fastapi import Depends, FastAPI, WebSocket
from fastapi.responses import JSONResponse
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send


# --------------------------------------------------------------------------- #
# Domain model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AuthFailure:
    status_code: int
    ws_code: int
    detail: str


class AuthError(Exception):
    def __init__(self, failure: AuthFailure) -> None:
        super().__init__(failure.detail)
        self.failure = failure


MISSING_CREDENTIALS = AuthFailure(401, 1008, "missing credentials")
FORBIDDEN = AuthFailure(403, 1008, "insufficient permission")
AUTH_SERVICE_DOWN = AuthFailure(503, 1011, "authentication service unavailable")


# --------------------------------------------------------------------------- #
# Yield dependency: task group is always cleaned up
# --------------------------------------------------------------------------- #


class SessionResource:
    """Pooled resource that owns its background task group.

    The task group is entered/exited by the resource itself rather than by the
    dependency generator.  That keeps the ``yield`` point in plain generator
    scope, so an auth exception raised by the caller is never funnelled through
    a task group boundary and therefore never becomes an ExceptionGroup (which
    would defeat exception-handler lookup by type).
    """

    def __init__(self) -> None:
        self.open = True
        self.heartbeat_cancelled = False
        self._task_group = anyio.create_task_group()
        self._lifetime = anyio.CancelScope()

    async def __aenter__(self) -> "SessionResource":
        await self._task_group.__aenter__()

        async def heartbeat() -> None:
            with self._lifetime:
                try:
                    await anyio.sleep_forever()
                except anyio.get_cancelled_exc_class():
                    self.heartbeat_cancelled = True
                    raise

        self._task_group.start_soon(heartbeat)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._lifetime.cancel()
        self.open = False
        await self._task_group.__aexit__(*exc_info)


async def auth_session():
    # Plain generator: the only thing that happens at teardown is an explicit
    # resource close.  No task group spans this yield, so AuthError keeps its
    # concrete type all the way to the registered exception handler.
    resource = await SessionResource().__aenter__()
    try:
        yield resource
    finally:
        await resource.__aexit__(None, None, None)


def decide(mode: str) -> dict[str, str]:
    """Pure policy decision; raises only the domain exception."""
    if mode == "ok":
        return {"sub": "user-42"}
    if mode == "missing":
        raise AuthError(MISSING_CREDENTIALS)
    if mode == "forbidden":
        raise AuthError(FORBIDDEN)
    raise AuthError(AUTH_SERVICE_DOWN)


async def get_principal(mode: str, session=Depends(auth_session)) -> dict[str, str]:
    # auth_session stays open for the whole request; FastAPI's per-route
    # AsyncExitStack closes it (cancelling the heartbeat) on every failure path.
    return decide(mode)


# --------------------------------------------------------------------------- #
# Scope-aware handler: one registration, two wire representations
# --------------------------------------------------------------------------- #


async def handle_auth_error(conn: Request | WebSocket, exc: AuthError) -> Response | None:
    failure = exc.failure
    if isinstance(conn, WebSocket):
        # A close frame is valid both pre- and post-accept and on every
        # compliant server; it never emits http.response.* denial events and
        # therefore does not depend on the optional denial-response extension.
        await conn.close(code=failure.ws_code, reason=failure.detail)
        return None
    return JSONResponse(status_code=failure.status_code, content={"error": failure.detail})


# --------------------------------------------------------------------------- #
# Minimal framework-gap fix: guard the denial-response extension
# --------------------------------------------------------------------------- #


class DenialResponseGuard:
    """Enforce the websocket.http.response extension rule at the ASGI boundary.

    Starlette's Response emits ``websocket.http.response.*`` without checking
    whether the server advertised the extension.  A compliant server would then
    raise and the handshake fails opaquely.  When the extension is absent this
    guard downgrades a denial to a plain ``websocket.close`` (1008), keeping the
    event sequence legal everywhere.

    It only normalises events from code *below* it, so it must wrap the route,
    not sit outside ExceptionMiddleware (whose handlers run inside the route).
    Mount it around a sub-application or individual route; the primary
    recommendation is to avoid denial responses entirely via handle_auth_error.
    """

    def __init__(self, app: ASGIApp, fallback_code: int = 1008) -> None:
        self.app = app
        self.fallback_code = fallback_code

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "websocket" or "websocket.http.response" in scope.get(
            "extensions", {}
        ):
            await self.app(scope, receive, send)
            return

        downgraded = False

        async def guarded_send(message: Message) -> None:
            nonlocal downgraded
            if message["type"] == "websocket.http.response.start":
                downgraded = True
                await send(
                    {"type": "websocket.close", "code": self.fallback_code, "reason": ""}
                )
                return
            if downgraded and message["type"] == "websocket.http.response.body":
                return
            await send(message)

        await self.app(scope, receive, guarded_send)


def create_app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(AuthError, handle_auth_error)  # type: ignore[arg-type]

    @app.get("/items")
    async def items(principal: dict = Depends(get_principal)) -> dict:
        return {"items": [], "owner": principal["sub"]}

    @app.websocket("/ws")
    async def ws_items(
        websocket: WebSocket,
        mode: str,
        principal: dict = Depends(get_principal),
    ) -> None:
        # An auth failure closes the socket before this body ever runs.
        await websocket.accept()
        await websocket.send_json({"items": [], "owner": principal["sub"]})
        await websocket.close(code=1000)

    return app


def guard_app(app: ASGIApp) -> DenialResponseGuard:
    """Wrap any ASGI app so denial events are legal without the extension."""
    return DenialResponseGuard(app)
