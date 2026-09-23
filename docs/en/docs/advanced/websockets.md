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

As this is a WebSocket it doesn't really make sense to raise an `HTTPException`, instead we raise a `WebSocketException`.

You can use a closing code from the [valid codes defined in the specification](https://tools.ietf.org/html/rfc6455#section-7.4.1).

///

## Dependency cleanup and exit timeline { #dependency-cleanup-and-exit-timeline }

Dependencies (including dependencies with `yield`) are solved **before** the WebSocket endpoint runs, so their setup code always runs before `await websocket.accept()`.

The exit code of dependencies with `yield` (the code after the `yield`) runs **after** the endpoint finishes, in reverse order of setup, and exactly once, no matter how the connection ends.

### Errors before the handshake { #errors-before-the-handshake }

If a dependency fails before `await websocket.accept()`, the handshake is rejected:

* `HTTPException` (including the ones raised by `Security` utilities): the handshake is rejected with the corresponding HTTP response, with the status code, headers, and `detail` from the exception.
* `WebSocketException`: the handshake is rejected with a WebSocket close using the exception's `code` and `reason`.
* Validation errors (e.g. a missing query parameter): the handshake is rejected with a WebSocket close with code `1008` and the validation errors as the reason.

/// note

When the handshake is rejected with a WebSocket close (instead of an `HTTPException`), the close code and reason are not delivered to the client, as the WebSocket protocol doesn't support them before the connection is established. The client only sees that the handshake was rejected (e.g. with an HTTP `403`). If you need the client to receive the error details, raise an `HTTPException` instead.

///

### Errors after the handshake { #errors-after-the-handshake }

If something fails after `await websocket.accept()`:

* `WebSocketException`: the connection is closed with the exception's `code` and `reason`.
* `HTTPException`: the connection is closed with code `1008` (for 4xx status codes) or `1011` (for 5xx status codes), with the `detail` as the reason.

### Disconnections, closing, and cancellation { #disconnections-closing-and-cancellation }

* **Client disconnection**: when the client disconnects, `WebSocketDisconnect` is raised in the endpoint. If the endpoint doesn't catch it, FastAPI handles it gracefully: the dependencies are cleaned up and nothing else is sent to the (already gone) client.
* **Endpoint closes the connection**: after `await websocket.close()`, the endpoint returns and the dependencies are cleaned up. FastAPI never sends a second close message, even if the dependency cleanup or an exception handler raises afterwards.
* **Cancellation**: if the server cancels the connection (e.g. during shutdown), the dependency cleanup runs once and the cancellation continues to propagate.
* **Errors in dependency cleanup**: if the exit code of a dependency with `yield` raises, the error propagates to your middleware, exception handlers, or the server, so you can detect and log it. If the connection was already closed, no second close message is sent.

### Background tasks { #websocket-background-tasks }

You can declare a `BackgroundTasks` parameter in a WebSocket endpoint. The background tasks run after the endpoint finishes, after dependencies with `scope="function"` are cleaned up, and before dependencies with `scope="request"` (the default) are cleaned up, the same order as for HTTP requests.

### Unsupported phases { #unsupported-phases }

Some things are not supported in WebSocket endpoints, because of the nature of the WebSocket protocol:

* The exit code of dependencies with `yield` runs after the connection is already closed, so it cannot `accept()`, send messages, or `close()` the connection again.
* `Response` parameters, response models, and HTTP status codes don't apply to WebSocket connections, use `WebSocketException` with a close code instead.

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
