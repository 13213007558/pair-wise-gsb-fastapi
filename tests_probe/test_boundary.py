"""Executable specification of the HTTP/WebSocket auth boundary.

Asserts raw ASGI events (not only TestClient exception names), the concrete
status/close codes, whether the business handler ran, and that the yield
dependency's resource + background task always exit.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse, WebSocketDisconnect

from auth_boundary_probe.app import (
    build_app,
    build_app_biz_message,
    build_app_http_only_handler,
)
from auth_boundary_probe.auth import FailureMode
from auth_boundary_probe.raw_asgi import (
    StrictASGIServerError,
    drive_http,
    drive_websocket,
    drive_websocket_strict,
)
from auth_boundary_probe.recommended import create_app, guard_app
from auth_boundary_probe.state import STATS

POLICY = {
    FailureMode.MISSING: (401, 1008),
    FailureMode.FORBIDDEN: (403, 1008),
    FailureMode.AUTH_DOWN: (503, 1011),
}
POLICY_FAILURES = [FailureMode.MISSING, FailureMode.FORBIDDEN]


# --------------------------------------------------------------------------- #
# HTTP transport
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", POLICY_FAILURES)
@pytest.mark.anyio
async def test_http_domain_and_httpex_return_status(mode):
    app = build_app()
    expected = POLICY[mode][0]
    for style in ("httpex", "domain"):
        STATS.reset()
        result = await drive_http(app, f"/http/{style}", f"mode={mode.value}")
        assert result.summary()["http_status"] == expected
        assert result.event_types == ["http.response.start", "http.response.body"]
        assert result.summary()["escaped_exc"] is None
        assert STATS.balanced, STATS


@pytest.mark.parametrize("mode", POLICY_FAILURES)
@pytest.mark.anyio
async def test_http_wsexc_is_a_type_mismatch(mode):
    # WebSocketException on an HTTP scope: the default ws handler receives a
    # Request and calls close(code=...), which Request does not accept ->
    # TypeError escapes and becomes a 500. Classic cross-transport type misuse.
    app = build_app()
    STATS.reset()
    result = await drive_http(app, "/http/wsexc", f"mode={mode.value}")
    assert result.summary()["escaped_exc"] == "TypeError"
    assert result.summary()["http_status"] == 500
    assert STATS.balanced


@pytest.mark.anyio
async def test_http_auth_service_error_escapes_unmapped():
    app = build_app()
    STATS.reset()
    result = await drive_http(app, "/http/domain", "mode=auth_down")
    assert result.summary()["escaped_exc"] == "AuthServiceError"
    assert STATS.balanced


def test_http_testclient_status_codes():
    app = build_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/http/httpex?mode=missing").status_code == 401
        assert client.get("/http/domain?mode=forbidden").status_code == 403
        assert client.get("/http/wsexc?mode=missing").status_code == 500


# --------------------------------------------------------------------------- #
# WebSocket: pre-accept
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", POLICY_FAILURES)
@pytest.mark.anyio
async def test_ws_pre_accept_raw_events(mode):
    app = build_app()
    ws_code = POLICY[mode][1]

    STATS.reset()
    httpex = await drive_websocket(app, "/ws/httpex/pre", f"mode={mode.value}")
    # HTTPException -> default http handler -> denial response events.
    assert httpex.event_types == [
        "websocket.http.response.start",
        "websocket.http.response.body",
    ]
    assert httpex.summary()["http_status"] == POLICY[mode][0]
    assert not any(e["type"] == "websocket.send" for e in httpex.events)
    assert STATS.balanced

    for style in ("wsexc", "domain"):
        STATS.reset()
        result = await drive_websocket(app, f"/ws/{style}/pre", f"mode={mode.value}")
        assert result.event_types == ["websocket.close"]
        assert result.events[0]["code"] == ws_code
        assert STATS.balanced


def test_ws_pre_accept_testclient_distinction():
    app = build_app()
    with TestClient(app) as client:
        with pytest.raises(WebSocketDenialResponse) as denial:
            with client.websocket_connect("/ws/httpex/pre?mode=missing"):
                pass
        assert denial.value.status_code == 401

        for style in ("wsexc", "domain"):
            with pytest.raises(WebSocketDisconnect) as closed:
                with client.websocket_connect(f"/ws/{style}/pre?mode=missing"):
                    pass
            assert closed.value.code == 1008


# --------------------------------------------------------------------------- #
# WebSocket: post-accept
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", POLICY_FAILURES)
@pytest.mark.anyio
async def test_ws_post_accept_raw_events(mode):
    app = build_app()

    # HTTPException after accept: the http handler emits denial events on an
    # already-accepted socket -> illegal state transition on a strict server.
    STATS.reset()
    httpex = await drive_websocket(app, "/ws/httpex/post", f"mode={mode.value}")
    assert httpex.event_types[0] == "websocket.accept"
    assert "websocket.http.response.start" in httpex.event_types
    assert not any(e["type"] == "websocket.send" for e in httpex.events)
    assert STATS.balanced

    for style in ("wsexc", "domain"):
        STATS.reset()
        result = await drive_websocket(app, f"/ws/{style}/post", f"mode={mode.value}")
        assert result.event_types == ["websocket.accept", "websocket.close"]
        assert result.events[1]["code"] == POLICY[mode][1]
        assert STATS.balanced


def test_ws_post_accept_testclient():
    app = build_app()
    with TestClient(app) as client:
        for style in ("wsexc", "domain"):
            with client.websocket_connect(f"/ws/{style}/post?mode=missing") as session:
                with pytest.raises(WebSocketDisconnect) as exc:
                    session.receive_json()
                assert exc.value.code == 1008


# --------------------------------------------------------------------------- #
# Framework protocol gap: denial extension is not checked by Starlette
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_denial_without_extension_rejected_by_strict_server():
    app = build_app_http_only_handler()
    strict = await drive_websocket_strict(
        app, "/ws/domain/pre", "mode=missing", denial_extension=False
    )
    assert isinstance(strict.escaped_exc, StrictASGIServerError)
    assert strict.events == []

    ok = await drive_websocket_strict(
        app, "/ws/domain/pre", "mode=missing", denial_extension=True
    )
    assert ok.event_types == [
        "websocket.http.response.start",
        "websocket.http.response.body",
    ]


# --------------------------------------------------------------------------- #
# Anti-pattern: auth failure turned into a normal business message
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_business_message_antipattern_leaks_data():
    app = build_app_biz_message()
    STATS.reset()
    result = await drive_websocket(app, "/ws-biz-message", "mode=missing")
    assert result.event_types == ["websocket.accept", "websocket.send", "websocket.close"]
    assert any(e["type"] == "websocket.send" for e in result.events)
    assert STATS.balanced


# --------------------------------------------------------------------------- #
# Recommended reusable pattern
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_recommended_ws_uses_close_even_without_extension():
    app = create_app()
    strict = await drive_websocket_strict(app, "/ws", "mode=missing", denial_extension=False)
    assert strict.escaped_exc is None
    assert strict.event_types == ["websocket.close"]
    assert strict.events[0]["code"] == 1008


@pytest.mark.anyio
async def test_guard_downgrades_denial_without_extension():
    guarded = guard_app(build_app_http_only_handler())
    strict = await drive_websocket_strict(
        guarded, "/ws/domain/pre", "mode=missing", denial_extension=False
    )
    assert strict.escaped_exc is None
    assert strict.event_types == ["websocket.close"]
    assert strict.events[0]["code"] == 1008


def test_recommended_http_and_ws_testclient():
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/items?mode=missing").status_code == 401
        assert client.get("/items?mode=forbidden").status_code == 403
        assert client.get("/items?mode=auth_down").status_code == 503
        assert client.get("/items?mode=ok").status_code == 200

        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws?mode=missing"):
                pass
        assert exc.value.code == 1008

        with client.websocket_connect("/ws?mode=ok") as session:
            assert session.receive_json()["owner"] == "user-42"


# --------------------------------------------------------------------------- #
# Structured-concurrency root cause: task group spanning the yield
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_task_group_spanning_yield_wraps_exception():
    """The anti-pattern dependency turns AuthError into an ExceptionGroup.

    A handler registered for the concrete type then cannot match, which is why
    the failure drifts to a 500 instead of the intended denial.
    """
    from contextlib import AsyncExitStack, asynccontextmanager

    from auth_boundary_probe.auth import (
        AuthError,
        FailureMode,
        managed_resource_spanning_yield,
    )

    stack = AsyncExitStack()
    await stack.enter_async_context(
        asynccontextmanager(managed_resource_spanning_yield)()
    )
    original = AuthError(FailureMode.MISSING, 401, 1008, "x")
    with pytest.raises(BaseException) as raised:
        await stack.__aexit__(type(original), original, original.__traceback__)
    assert type(raised.value).__name__ == "ExceptionGroup"
    assert not isinstance(raised.value, AuthError)


@pytest.mark.anyio
async def test_recommended_resource_stays_open_then_cleans():
    app = create_app()
    STATS.reset()
    result = await drive_websocket(app, "/ws", "mode=ok")
    assert result.event_types[0] == "websocket.accept"
    assert any(e["type"] == "websocket.send" for e in result.events)
    assert result.escaped_exc is None
