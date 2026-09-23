import json

from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError, WebSocketRequestValidationError
from fastapi.utils import is_body_allowed_for_status_code
from fastapi.websockets import WebSocket
from starlette.exceptions import HTTPException
from starlette.exceptions import (
    WebSocketException as StarletteWebSocketException,
)
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.status import WS_1008_POLICY_VIOLATION


async def http_exception_handler(request: Request, exc: HTTPException) -> Response:
    headers = getattr(exc, "headers", None)
    if not is_body_allowed_for_status_code(exc.status_code):
        return Response(status_code=exc.status_code, headers=headers)
    return JSONResponse(
        {"detail": exc.detail}, status_code=exc.status_code, headers=headers
    )


async def request_validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(exc.errors())},
    )


async def websocket_request_validation_exception_handler(
    websocket: WebSocket, exc: WebSocketRequestValidationError
) -> None:
    if "fastapi.websocket_close_sent" in websocket.scope:
        raise exc
    reason = json.dumps(jsonable_encoder(exc.errors()), separators=(",", ":"))
    encoded_reason = reason.encode("utf-8")[:123]
    await websocket.close(
        code=WS_1008_POLICY_VIOLATION,
        reason=encoded_reason.decode("utf-8", errors="ignore"),
    )


async def websocket_exception_handler(websocket: WebSocket, exc: Exception) -> None:
    assert isinstance(exc, StarletteWebSocketException)
    if "fastapi.websocket_close_sent" in websocket.scope:
        raise exc
    await websocket.close(code=exc.code, reason=exc.reason)
