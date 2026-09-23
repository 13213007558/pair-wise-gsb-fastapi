from starlette.types import Message
from starlette.websockets import WebSocket as StarletteWebSocket
from starlette.websockets import WebSocketDisconnect as WebSocketDisconnect  # noqa
from starlette.websockets import WebSocketState as WebSocketState  # noqa


class WebSocket(StarletteWebSocket):
    async def send(self, message: Message) -> None:
        if message["type"] == "websocket.close":
            self.scope["fastapi.websocket_close_sent"] = {
                "code": message.get("code", 1000),
                "reason": message.get("reason", ""),
            }
        await super().send(message)


__all__ = ["WebSocket", "WebSocketDisconnect", "WebSocketState"]
