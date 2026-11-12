# FastAPI 认证边界：HTTP 与 WebSocket 共用同一份依赖

环境：FastAPI 0.162.3 / Starlette 1.10.0 / AnyIO 4.16 / Python 3.12。
本目录是一套可执行探针，结论都由 `tests_probe/test_boundary.py` 与
`run_matrix.py` 实测得到，而不是只看 `TestClient` 抛出的异常名。

## 怎么跑

```bash
.venv/bin/python -m pytest tests_probe -q            # 19 项断言
.venv/bin/python -m auth_boundary_probe.run_matrix   # 打印完整事件矩阵(Markdown)
.venv/bin/python -m auth_boundary_probe.deep_dive    # 扩展/握手的聚焦验证
```

## 中间件与异常处理的实际装配顺序

`FastAPI.build_middleware_stack()`（`fastapi/applications.py:1032`）从外到内：

```
ServerErrorMiddleware -> 用户中间件 -> ExceptionMiddleware -> AsyncExitStackMiddleware -> 路由
```

两个关键事实（读源码确认）：

- `ServerErrorMiddleware.__call__` 对 `scope["type"] != "http"` 直接透传，
  既不生成 500 响应也不兜底（`starlette/middleware/errors.py:150`）。所以
  WebSocket 上未被处理的异常会**带着原类型逃逸出应用**，没有任何 ASGI 事件。
- `ExceptionMiddleware` 同时注册了 `HTTPException` 和 `WebSocketException`
  处理器，并按 scope 传入 `Request` 或 `WebSocket`
  （`starlette/middleware/exceptions.py:26`、`_exception_handler.py:20`）。
  WebSocket 路由自身又在 `fastapi/routing.py:173` 的 `websocket_session` 里
  再包了一层同样的异常处理，因此 WS 依赖/端点抛出的异常在路由内即被处置。

## 内存调用对照（实测）

记号：denial = `websocket.http.response.start/body`（HTTP 形态拒绝，需要
`websocket.http.response` 扩展）；close = `websocket.close`（任意服务器都支持）。

### HTTP 路由

| 依赖抛出 | ASGI 事件 | 状态码 | 逃逸异常 | 业务体执行 |
|---|---|---|---|---|
| `HTTPException` | http.response.start/body | 401/403/503 | 无 | 否 |
| `WebSocketException` | http.response.start/body | **500** | `TypeError` | 否 |
| 域异常 `AuthError`（自定义处理器） | http.response.start/body | 401/403/503 | 无 | 否 |
| 未映射的基础设施异常 `AuthServiceError` | （仅 500，由外层兜底） | 500 | `AuthServiceError` | 否 |

`WebSocketException` 用在 HTTP 上为何变 500：默认 ws 处理器拿到的是 `Request`，
却调用 `websocket.close(code=...)`，而 `Request.close()` 不接受 `code` 关键字，
抛 `TypeError: Request.close() got an unexpected keyword argument 'code'`，
再被外层记成 500。这是**异常类型用错边界**，不是框架 bug。

### WebSocket 路由，accept 之前（application_state=CONNECTING）

| 依赖抛出 | ASGI 事件 | 实际拒绝 | TestClient 表现 | 业务体执行 |
|---|---|---|---|---|
| `HTTPException` | denial start/body | HTTP 401/403/503 | `WebSocketDenialResponse(status=...)` | 否 |
| `WebSocketException` | `websocket.close` | close 1008/1011 | `WebSocketDisconnect(code=...)` | 否 |
| 域异常 `AuthError`（自定义处理器） | `websocket.close` | close 1008/1011 | `WebSocketDisconnect(code=...)` | 否 |
| 未映射 `AuthServiceError` | **无事件** | 异常直接逃逸 | 原样抛出 `AuthServiceError` | 否 |

### WebSocket 路由，accept 之后（application_state=CONNECTED）

| 依赖抛出 | ASGI 事件 | 结果 | TestClient 表现 | 业务体执行 |
|---|---|---|---|---|
| `HTTPException` | accept **之后再发 denial 事件** | 协议状态违规 | 连接已建立，读时抛 `WebSocketDenialResponse` | 否 |
| `WebSocketException` | accept, close | 干净 close 1008/1011 | 读时 `WebSocketDisconnect` | 否 |
| 域异常 `AuthError` | accept, close | 干净 close 1008/1011 | 读时 `WebSocketDisconnect` | 否 |

accept 之后再抛 `HTTPException` 是最危险的一种：升级已经成功，HTTP 形态的
denial 事件却被发往一个已连接的 socket。`TestClient` 因为同时实现了 denial
扩展而“看起来能收到响应”，但真实服务器上这是非法状态转移——这正是题目里
“有时握手失败、有时运行时错误”的来源。

### 两个反例（实测）

- HTTP-only 自定义处理器复用在 WS：无 `websocket.http.response` 扩展时，
  Starlette 的 `Response.__call__` 仍发 denial 事件（`starlette/responses.py:168`
  不检查扩展）。用合规服务器的 send 包装（`raw_asgi.strict_server_send`）实测
  直接抛 `StrictASGIServerError`，握手不产生任何事件。
- 把认证失败降级成业务消息（`/ws-biz-message`）：事件序列是
  `accept, websocket.send, close`，业务体确实执行、客户端收到的是普通数据帧，
  认证失败被伪装成成功升级后的消息——题目明确禁止的做法。

## yield 依赖与任务清理：一个隐蔽的根因

`fastapi/routing.py:173` 的每个 WS 路由、`request_response`（`:129`）的每个
HTTP 路由都各自持有 `AsyncExitStack`（`fastapi_inner_astack`），yield 依赖在
求解阶段进入、在路由退出时关闭。实测所有失败路径下资源都能进入/退出配对、
后台心跳都被取消（矩阵 `teardown ok=True`）。

但有一个结构化陷阱（`test_task_group_spanning_yield_wraps_exception` 固化）：

- 若 yield 点落在 `anyio.create_task_group()` **内部**，请求处理期间抛出的非
  取消异常，在任务组退出时会被 AnyIO 重新打包成 `ExceptionGroup`。于是按
  具体类型（如 `AuthError`）注册的处理器查表失败，本应 401 的请求变成 500。
- 对照实测（同一异常、两种依赖形态）：纯生成器 → **401**；任务组跨 yield →
  **500（ExceptionGroup）**。

正确形态见 `recommended.py`：让**资源自己持有任务组**（在 `__aenter__` 进入、
`__aexit__` 取消并 drain），依赖生成器的 yield 处于普通生成器作用域，不被任何
任务组边界包裹。这样异常保持原类型，后台任务又能跨整个请求存活并被确定性取消。

## 推荐的异常使用方式

1. 依赖只抛一个与传输无关的域异常 `AuthError`，绝不 import
   `HTTPException`/`WebSocketException`，从而可被 HTTP 与 WS 安全共用。
2. 只注册**一个**处理器，按连接类型分发（`recommended.handle_auth_error`）：
   - HTTP：返回 `JSONResponse`，使用映射的状态码；
   - WebSocket：无论 accept 前后都 `websocket.close(code, reason)`。close 在
     CONNECTING 与 CONNECTED 状态都合法，且不依赖 denial 扩展，兼容性最好。
3. 认证服务自身故障（5xx 类）应显式建模并映射到 503 / close 1011，而不是让
   裸异常逃逸；未映射异常在 WS 上不会产生任何关闭帧，客户端只能感知到断连。
4. 绝不在 accept 后把认证失败作为普通业务 JSON 发送。
5. yield 资源采用“资源自持任务组、yield 不跨任务组”的结构，避免 ExceptionGroup
   吞掉具体异常类型。

## 框架层面的协议缺口与最小修复

框架在当前版本**违反了一条 ASGI 约定**：`Response` 在 websocket scope 下发送
`websocket.http.response.*` 前不检查 scope 是否声明了
`websocket.http.response` 扩展（`starlette/responses.py:168`）。这不是异常类型
误用，而是框架事件协议缺口；在不支持该扩展的服务器上会导致握手期硬失败。

首选规避：按上面的推荐，WS 分支只用 `websocket.close`，根本不发 denial 事件。

若必须兼容“返回 HTTP 响应拒绝 WebSocket”的既有代码，`recommended.py` 提供了
最小 ASGI 中间件 `DenialResponseGuard`（`guard_app()` 包装）：当 scope 未声明
扩展时，把 `websocket.http.response.start/body` 降级为单个
`websocket.close(1008)`，保证事件序列在任何服务器上都合法。注意它必须包裹在
产生 denial 事件的应用/路由外侧，而不是放在 ExceptionMiddleware 之外的全局
用户中间件（那里已经捕获不到路由内的异常）。

## 文件说明

- `auth.py`：认证决策核心、三种异常风格依赖、正确/错误两种 yield 资源形态。
- `app.py`：HTTP/WS 路由（accept 前/后）、自定义处理器、两个反例工厂。
- `raw_asgi.py`：裸 ASGI3 驱动器与合规服务器 send 包装，记录真实事件。
- `run_matrix.py`：全量矩阵，输出 JSON + Markdown。
- `deep_dive.py`：denial 扩展与 TestClient 表现的聚焦验证。
- `recommended.py`：可复用的推荐接入范例 + 协议守卫中间件。
- `../tests_probe/test_boundary.py`：19 项可执行断言。
