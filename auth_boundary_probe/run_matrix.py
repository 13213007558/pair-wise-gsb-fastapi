"""Run the full boundary matrix and print a comparison table.

For every (transport, style, failure, timing) cell it records:
  * raw ASGI output events and any exception escaping the stack,
  * the concrete HTTP status or WebSocket close code,
  * whether the business handler executed,
  * whether the yield dependency's resource/background task exited cleanly.

Run:  python -m auth_boundary_probe.run_matrix
"""

from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse, WebSocketDisconnect

from .app import build_app, build_app_biz_message, build_app_http_only_handler
from .auth import FailureMode
from .raw_asgi import drive_http, drive_websocket, drive_websocket_strict
from .state import STATS

FAILURES = (FailureMode.MISSING, FailureMode.FORBIDDEN, FailureMode.AUTH_DOWN)
HTTP_STYLES = ("httpex", "wsexc", "domain")
WS_STYLES = ("httpex", "wsexc", "domain")
WHS = ("pre", "post")


def _business_ran(events: list[dict]) -> bool:
    return any(event["type"] == "websocket.send" for event in events)


async def _http_raw(app, style: str, mode: FailureMode) -> dict:
    STATS.reset()
    result = await drive_http(app, f"/http/{style}", f"mode={mode.value}")
    summary = result.summary()
    summary["business_ran"] = summary["http_status"] == 200
    summary["teardown_balanced"] = STATS.balanced
    return summary


def _http_tc(app, style: str, mode: FailureMode) -> str:
    with TestClient(app, raise_server_exceptions=False) as client:
        try:
            response = client.get(f"/http/{style}?mode={mode.value}")
            return f"HTTP {response.status_code}"
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}"


async def _ws_raw(app, style: str, when: str, mode: FailureMode, *, ext: bool = True) -> dict:
    STATS.reset()
    result = await drive_websocket(
        app, f"/ws/{style}/{when}", f"mode={mode.value}", denial_extension=ext
    )
    summary = result.summary()
    summary["business_ran"] = _business_ran(result.events)
    summary["teardown_balanced"] = STATS.balanced
    return summary


def _ws_tc(app, style: str, when: str, mode: FailureMode) -> str:
    with TestClient(app) as client:
        try:
            with client.websocket_connect(
                f"/ws/{style}/{when}?mode={mode.value}"
            ) as session:
                try:
                    session.receive_json()
                    return "connected+data"
                except WebSocketDenialResponse as exc:
                    return f"WebSocketDenialResponse({exc.status_code})"
                except WebSocketDisconnect as exc:
                    return f"WebSocketDisconnect({exc.code})"
        except WebSocketDenialResponse as exc:
            return f"WebSocketDenialResponse({exc.status_code})"
        except WebSocketDisconnect as exc:
            return f"WebSocketDisconnect({exc.code})"
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}"


async def main() -> None:
    app = build_app()
    rows: list[dict] = []

    for mode in FAILURES:
        for style in HTTP_STYLES:
            row = await _http_raw(app, style, mode)
            row["testclient_outcome"] = _http_tc(app, style, mode)
            rows.append(
                {"transport": "http", "style": style, "when": "-", "mode": mode.value, **row}
            )

    for mode in FAILURES:
        for when in WHS:
            for style in WS_STYLES:
                row = await _ws_raw(app, style, when, mode)
                row["testclient_outcome"] = _ws_tc(app, style, when, mode)
                rows.append(
                    {"transport": "ws", "style": style, "when": when, "mode": mode.value, **row}
                )

    # Anti-pattern A: HTTP-only custom handler, with vs without denial extension.
    http_only = build_app_http_only_handler()
    for ext in (True, False):
        for mode in FAILURES:
            STATS.reset()
            strict = await drive_websocket_strict(
                http_only, "/ws/domain/pre", f"mode={mode.value}", denial_extension=ext
            )
            rows.append(
                {
                    "transport": "ws",
                    "style": f"http-only-handler ext={ext}",
                    "when": "pre",
                    "mode": mode.value,
                    **strict.summary(),
                    "business_ran": _business_ran(strict.events),
                    "teardown_balanced": STATS.balanced,
                    "testclient_outcome": "-",
                }
            )

    # Anti-pattern B: auth failure downgraded to a business message post-accept.
    biz = build_app_biz_message()
    for mode in FAILURES:
        STATS.reset()
        result = await drive_websocket(biz, "/ws-biz-message", f"mode={mode.value}")
        rows.append(
            {
                "transport": "ws",
                "style": "biz-message",
                "when": "post",
                "mode": mode.value,
                **result.summary(),
                "business_ran": _business_ran(result.events),
                "teardown_balanced": STATS.balanced,
                "testclient_outcome": "-",
            }
        )

    print(json.dumps(rows, indent=2, ensure_ascii=False))
    _print_markdown(rows)


def _print_markdown(rows: list[dict]) -> None:
    print("\n# Markdown matrix\n")
    print(
        "| transport | style | when | failure | ASGI events | status/close | "
        "escaped exc | business ran | teardown ok | TestClient |"
    )
    print("|" + "---|" * 10)
    for row in rows:
        wire = (
            row["http_status"]
            if row.get("http_status") is not None
            else row.get("close_code")
        )
        print(
            "| {transport} | {style} | {when} | {mode} | {events} | {wire} | {exc} | "
            "{biz} | {teardown} | {tc} |".format(
                transport=row["transport"],
                style=row["style"],
                when=row["when"],
                mode=row["mode"],
                events=", ".join(row["events"]),
                wire=wire,
                exc=row.get("escaped_exc") or "-",
                biz=row.get("business_ran", "-"),
                teardown=row.get("teardown_balanced", "-"),
                tc=row.get("testclient_outcome", "-"),
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
