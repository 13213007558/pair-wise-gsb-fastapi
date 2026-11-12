"""FastAPI application under test.

One authentication core, exposed over both HTTP and WebSocket with three
exception styles and two failure timings (before/after ``accept``).  All auth
entry points use FastAPI ``Depends`` so the yield sub-dependency is genuinely
managed by the per-route AsyncExitStack.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, WebSocket
from fastapi.responses import JSONResponse
from starlette.exceptions import WebSocketException
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from .auth import (
    AuthError,
    FailureMode,
    authenticate,
    managed_resource,
)

router = APIRouter()


# --------------------------------------------------------------------------- #
# Auth dependency factories: same decision core, three exception styles.
# --------------------------------------------------------------------------- #


async def _http_principal(
    mode: FailureMode, resource: dict = Depends(managed_resource)
) -> dict:
    from starlette.exceptions import HTTPException

    try:
        return authenticate(mode)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


async def _ws_exception_principal(
    mode: FailureMode, resource: dict = Depends(managed_resource)
) -> dict:
    try:
        return authenticate(mode)
    except AuthError as exc:
        raise WebSocketException(code=exc.ws_code, reason=exc.detail) from exc


async def _domain_principal(
    mode: FailureMode, resource: dict = Depends(managed_resource)
) -> dict:
    return authenticate(mode)


@router.get("/http/httpex")
async def http_httpex_route(principal: dict = Depends(_http_principal)) -> dict:
    return {"ok": True, "principal": principal}


@router.get("/http/wsexc")
async def http_wsexc_route(principal: dict = Depends(_ws_exception_principal)) -> dict:
    return {"ok": True, "principal": principal}


@router.get("/http/domain")
async def http_domain_route(principal: dict = Depends(_domain_principal)) -> dict:
    return {"ok": True, "principal": principal}


# --------------------------------------------------------------------------- #
# WebSocket routes.
#
# pre  -> dependency solved while application_state is CONNECTING (before accept)
# post -> socket accepted first, then an explicit authenticated step runs.
# --------------------------------------------------------------------------- #


@router.websocket("/ws/httpex/pre")
async def ws_httpex_pre(
    websocket: WebSocket, principal: dict = Depends(_http_principal)
) -> None:
    await websocket.accept()
    await websocket.send_json({"ok": True, "principal": principal})
    await websocket.close(code=1000)


@router.websocket("/ws/wsexc/pre")
async def ws_wsexc_pre(
    websocket: WebSocket, principal: dict = Depends(_ws_exception_principal)
) -> None:
    await websocket.accept()
    await websocket.send_json({"ok": True, "principal": principal})
    await websocket.close(code=1000)


@router.websocket("/ws/domain/pre")
async def ws_domain_pre(
    websocket: WebSocket, principal: dict = Depends(_domain_principal)
) -> None:
    await websocket.accept()
    await websocket.send_json({"ok": True, "principal": principal})
    await websocket.close(code=1000)


# Post-accept: accept first, then run the same decision inside the handler.
# These call the decision core directly (resource still injected) so the failure
# happens after the upgrade.


@router.websocket("/ws/httpex/post")
async def ws_httpex_post(
    websocket: WebSocket,
    mode: FailureMode,
    resource: dict = Depends(managed_resource),
) -> None:
    await websocket.accept()
    try:
        principal = authenticate(mode)
    except AuthError as exc:
        from starlette.exceptions import HTTPException

        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    await websocket.send_json({"ok": True, "principal": principal})
    await websocket.close(code=1000)


@router.websocket("/ws/wsexc/post")
async def ws_wsexc_post(
    websocket: WebSocket,
    mode: FailureMode,
    resource: dict = Depends(managed_resource),
) -> None:
    await websocket.accept()
    try:
        principal = authenticate(mode)
    except AuthError as exc:
        raise WebSocketException(code=exc.ws_code, reason=exc.detail) from exc
    await websocket.send_json({"ok": True, "principal": principal})
    await websocket.close(code=1000)


@router.websocket("/ws/domain/post")
async def ws_domain_post(
    websocket: WebSocket,
    mode: FailureMode,
    resource: dict = Depends(managed_resource),
) -> None:
    await websocket.accept()
    # Raising the domain exception after accept is handled by the same handler.
    principal = authenticate(mode)
    await websocket.send_json({"ok": True, "principal": principal})
    await websocket.close(code=1000)


# --------------------------------------------------------------------------- #
# Recommended scope-aware handler for the domain AuthError.
# --------------------------------------------------------------------------- #


async def auth_error_dispatch(
    conn: Request | WebSocket, exc: AuthError
) -> JSONResponse | None:
    if isinstance(conn, WebSocket):
        await conn.close(code=exc.ws_code, reason=exc.detail)
        return None
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


def build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(AuthError, auth_error_dispatch)  # type: ignore[arg-type]
    return app


# --------------------------------------------------------------------------- #
# Anti-pattern A: an HTTP-only custom handler reused on WebSocket.  Emits
# denial events regardless of whether the server supports the extension.
# --------------------------------------------------------------------------- #


async def auth_error_http_only(request: Request, exc: AuthError) -> PlainTextResponse:
    return PlainTextResponse(status_code=exc.status_code, content=exc.detail)


def build_app_http_only_handler() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(AuthError, auth_error_http_only)  # type: ignore[arg-type]
    return app


# --------------------------------------------------------------------------- #
# Anti-pattern B: swallow the auth failure after accept and push it as a normal
# business JSON message. The upgrade succeeds and the client gets data instead
# of a denial/close.
# --------------------------------------------------------------------------- #


@router.websocket("/ws-biz-message")
async def ws_business_message_route(
    websocket: WebSocket,
    mode: FailureMode,
    resource: dict = Depends(managed_resource),
) -> None:
    await websocket.accept()
    try:
        principal = authenticate(mode)
    except AuthError as exc:
        await websocket.send_json({"type": "auth_error", "detail": exc.detail})
        await websocket.close(code=1000)
        return
    await websocket.send_json({"ok": True, "principal": principal})
    await websocket.close(code=1000)


def build_app_biz_message() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(AuthError, auth_error_dispatch)  # type: ignore[arg-type]
    return app
