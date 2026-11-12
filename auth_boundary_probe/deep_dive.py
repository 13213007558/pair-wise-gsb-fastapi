"""Focused checks the broad matrix cannot show clearly."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse, WebSocketDisconnect

from .app import build_app, build_app_http_only_handler
from .raw_asgi import drive_websocket, drive_websocket_strict


async def no_extension_denial() -> None:
    app = build_app_http_only_handler()
    print("== HTTP-only custom handler, NO denial extension (strict server) ==")
    strict = await drive_websocket_strict(
        app, "/ws/domain/pre", "mode=missing", denial_extension=False
    )
    print(json.dumps(strict.summary(), indent=2))
    print("strict server raised:", type(strict.escaped_exc).__name__)


async def default_httpex_extension_matrix() -> None:
    app = build_app()
    for ext in (False, True):
        print(f"== HTTPException on WS pre-accept, extension={ext} ==")
        result = await drive_websocket_strict(
            app, "/ws/httpex/pre", "mode=missing", denial_extension=ext
        )
        print(json.dumps(result.summary(), indent=2))


def testclient_pre_accept() -> None:
    app = build_app()
    print("== TestClient pre-accept: denial vs close ==")
    with TestClient(app) as client:
        try:
            with client.websocket_connect("/ws/httpex/pre?mode=missing"):
                pass
        except WebSocketDenialResponse as exc:
            print("httpex -> WebSocketDenialResponse status:", exc.status_code)
        for style in ("wsexc", "domain"):
            try:
                with client.websocket_connect(f"/ws/{style}/pre?mode=missing"):
                    pass
            except WebSocketDisconnect as exc:
                print(style, "-> WebSocketDisconnect code:", exc.code)


def testclient_post_accept() -> None:
    app = build_app()
    print("== TestClient post-accept ==")
    with TestClient(app) as client:
        with client.websocket_connect("/ws/wsexc/post?mode=missing") as session:
            try:
                session.receive_json()
            except WebSocketDisconnect as exc:
                print("wsexc -> WebSocketDisconnect code:", exc.code)
        with client.websocket_connect("/ws/httpex/post?mode=missing") as session:
            try:
                session.receive_json()
            except WebSocketDenialResponse as exc:
                print("httpex -> WebSocketDenialResponse status:", exc.status_code)


if __name__ == "__main__":
    import asyncio

    asyncio.run(no_extension_denial())
    asyncio.run(default_httpex_extension_matrix())
    testclient_pre_accept()
    testclient_post_accept()
