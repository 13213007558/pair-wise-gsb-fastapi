import json

from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError, WebSocketRequestValidationError
from fastapi.utils import is_body_allowed_for_status_code
from fastapi.websockets import WebSocket
from starlette.exceptions import HTTPException, WebSocketException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.status import WS_1008_POLICY_VIOLATION, WS_1011_INTERNAL_ERROR
from starlette.websockets import WebSocketState


async def http_exception_handler(
    request: Request | WebSocket, exc: HTTPException
) -> Response | None:
    headers = getattr(exc, "headers", None)
    if request.scope["type"] == "websocket":
        assert isinstance(request, WebSocket)
        if request.application_state == WebSocketState.CONNECTED:
            # The WebSocket handshake already happened, an HTTP response can no
            # longer be sent, close the WebSocket connection instead
            code = (
                WS_1008_POLICY_VIOLATION
                if exc.status_code < 500
                else WS_1011_INTERNAL_ERROR
            )
            await request.close(code=code, reason=str(exc.detail))
            return None
        if request.application_state != WebSocketState.CONNECTING:
            # The WebSocket connection is already closed, don't send a second
            # close message
            return None
        # The handshake hasn't happened yet, reject it with an HTTP response
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
    if websocket.application_state in (
        WebSocketState.CONNECTING,
        WebSocketState.CONNECTED,
    ):
        await websocket.close(
            code=WS_1008_POLICY_VIOLATION,
            reason=json.dumps(jsonable_encoder(exc.errors())),
        )


async def websocket_exception_handler(
    websocket: WebSocket, exc: WebSocketException
) -> None:
    if websocket.application_state in (
        WebSocketState.CONNECTING,
        WebSocketState.CONNECTED,
    ):
        await websocket.close(code=exc.code, reason=exc.reason)
