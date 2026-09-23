# WebSockets { #websockets }

You can use [WebSockets](https://developer.mozilla.org/en-US/docs/Web/API/WebSockets_API) with **FastAPI**.

## Install `websockets` { #install-websockets }

Add `websockets` (a Python library that makes it easy to use the "WebSocket" protocol) to your project:

<div class="termy">

```console
$ uv add websockets

---> 100%
```

</div>

## WebSockets client { #websockets-client }

### In production { #in-production }

In your production system, you probably have a frontend created with a modern framework like React, Vue.js or Angular.

And to communicate using WebSockets with your backend you would probably use your frontend's utilities.

Or you might have a native mobile application that communicates with your WebSocket backend directly, in native code.

Or you might have any other way to communicate with the WebSocket endpoint.

---

But for this example, we'll use a very simple HTML document with some JavaScript, all inside a long string.

This, of course, is not optimal and you wouldn't use it for production.

In production you would have one of the options above.

But it's the simplest way to focus on the server-side of WebSockets and have a working example:

{* ../../docs_src/websockets_/tutorial001_py310.py hl[2,6:38,41:43] *}

## Create a `websocket` { #create-a-websocket }

In your **FastAPI** application, create a `websocket`:

{* ../../docs_src/websockets_/tutorial001_py310.py hl[1,46:47] *}

/// note | Technical Details

You could also use `from starlette.websockets import WebSocket`.

**FastAPI** provides the same `WebSocket` directly just as a convenience for you, the developer. But it comes directly from Starlette.

///

## Await for messages and send messages { #await-for-messages-and-send-messages }

In your WebSocket route you can `await` for messages and send messages.

{* ../../docs_src/websockets_/tutorial001_py310.py hl[48:52] *}

You can receive and send binary, text, and JSON data.

## Try it { #try-it }

Put your code in a file `main.py` and then run your application:

<div class="termy">

```console
$ uv run fastapi dev

<span style="color: green;">INFO</span>:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

</div>

Open your browser at [http://127.0.0.1:8000](http://127.0.0.1:8000).

You will see a simple page like:

<img src="/img/tutorial/websockets/image01.png">

You can type messages in the input box, and send them:

<img src="/img/tutorial/websockets/image02.png">

And your **FastAPI** application with WebSockets will respond back:

<img src="/img/tutorial/websockets/image03.png">

You can send (and receive) many messages:

<img src="/img/tutorial/websockets/image04.png">

And all of them will use the same WebSocket connection.

## Using `Depends` and others { #using-depends-and-others }

In WebSocket endpoints you can import from `fastapi` and use:

* `Depends`
* `Security`
* `Cookie`
* `Header`
* `Path`
* `Query`

They work the same way as for other FastAPI endpoints/*path operations*:

{* ../../docs_src/websockets_/tutorial002_an_py310.py hl[68:69,82] *}

/// note

For a WebSocket-specific close code and reason, raise a `WebSocketException`. An `HTTPException` before acceptance is returned as an HTTP denial response when supported, and after acceptance is mapped to WebSocket close code `1008`.

You can use a closing code from the [valid codes defined in the specification](https://tools.ietf.org/html/rfc6455#section-7.4.1).

///

## Dependencies and connection lifecycle { #dependencies-and-connection-lifecycle }

FastAPI resolves all dependencies before your endpoint calls `await websocket.accept()` or closes the handshake. Dependencies with `yield` use two boundaries:

* `scope="function"` dependencies enter before the endpoint and exit after the endpoint function returns.
* `scope="request"` dependencies also enter before the endpoint, but exit after function-scoped dependencies and WebSocket background tasks finish.
* The handshake is accepted only when application code calls `websocket.accept()`. FastAPI does not implicitly accept a connection.

| Timeline | Dependency enter/exit | Client result | Error source and notes |
|---|---|---|---|
| Rejected before `accept()` | Request-scoped and function-scoped dependencies entered; failed dependencies unwind | `HTTPException` sends an HTTP denial response; `WebSocketException` and validation errors send a WebSocket close | Use `WebSocketException` for a WebSocket close code/reason. An HTTP denial response requires server support for the WebSocket Denial Response extension. |
| Client disconnects after `accept()` | Both stacks unwind; function scope first, then request scope | The client's disconnect code/reason is retained on `WebSocketDisconnect`; FastAPI does not send a second close frame | The original `WebSocketDisconnect` propagates after cleanup. Middleware may catch it, but cleanup errors can still observe it. |
| Endpoint closes the connection | Cleanup runs after the endpoint-initiated close | The endpoint's close code/reason is the only close frame sent | A cleanup exception is raised instead of replacing or swallowing the endpoint's close. |
| Endpoint raises after `accept()` | Cleanup runs while the exception propagates | `HTTPException` maps to `1008` with its detail as reason; `WebSocketException` keeps its code/reason; validation errors use `1008` | Generic exceptions remain generic exceptions so custom middleware can handle them. |
| Connection is canceled | Cleanup runs while cancellation propagates | No framework-generated close frame is sent | Put cancellation-safe teardown in `finally` blocks. |

After the endpoint and all cleanups finish, if the connection was accepted but neither the endpoint nor cleanup sent a close frame, FastAPI sends one normal close. This is the only implicit WebSocket close.

/// note
After `websocket.close()` has been called, the connection cannot send another close frame. Use cleanup code to release resources; do not use it to replace a close code or reason already selected by the endpoint.
///

/// warning
Background tasks run after function-scoped cleanup and before request-scoped cleanup. A task cannot change the WebSocket handshake, and sending messages from a background task after the endpoint closed the connection is not supported.
///

### Try the WebSockets with dependencies { #try-the-websockets-with-dependencies }

Run your application:

<div class="termy">

```console
$ uv run fastapi dev

<span style="color: green;">INFO</span>:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

</div>

Open your browser at [http://127.0.0.1:8000](http://127.0.0.1:8000).

There you can set:

* The "Item ID", used in the path.
* The "Token" used as a query parameter.

/// tip

Notice that the query `token` will be handled by a dependency.

///

With that you can connect the WebSocket and then send and receive messages:

<img src="/img/tutorial/websockets/image05.png">

## Handling disconnections and multiple clients { #handling-disconnections-and-multiple-clients }

When a WebSocket connection is closed, the `await websocket.receive_text()` will raise a `WebSocketDisconnect` exception, which you can then catch and handle like in this example.

{* ../../docs_src/websockets_/tutorial003_py310.py hl[79:81] *}

To try it out:

* Open the app with several browser tabs.
* Write messages from them.
* Then close one of the tabs.

That will raise the `WebSocketDisconnect` exception, and all the other clients will receive a message like:

```
Client #1596980209979 left the chat
```

/// tip

The app above is a minimal and simple example to demonstrate how to handle and broadcast messages to several WebSocket connections.

But keep in mind that, as everything is handled in memory, in a single list, it will only work while the process is running, and will only work with a single process.

If you need something easy to integrate with FastAPI but that is more robust, supported by Redis, PostgreSQL or others, check [encode/broadcaster](https://github.com/encode/broadcaster).

///

## More info { #more-info }

To learn more about the options, check Starlette's documentation for:

* [The `WebSocket` class](https://starlette.dev/websockets/).
* [Class-based WebSocket handling](https://starlette.dev/endpoints/#websocketendpoint).
