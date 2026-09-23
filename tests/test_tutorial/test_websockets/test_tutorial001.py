from fastapi.testclient import TestClient

from docs_src.websockets_.tutorial001_py310 import app

client = TestClient(app)


def test_main():
    response = client.get("/")
    assert response.status_code == 200, response.text
    assert b"<!DOCTYPE html>" in response.content


def test_websocket():
    # The endpoint doesn't handle WebSocketDisconnect itself, FastAPI handles
    # the disconnection gracefully when the client closes the connection
    with client.websocket_connect("/ws") as websocket:
        message = "Message one"
        websocket.send_text(message)
        data = websocket.receive_text()
        assert data == f"Message text was: {message}"
        message = "Message two"
        websocket.send_text(message)
        data = websocket.receive_text()
        assert data == f"Message text was: {message}"
