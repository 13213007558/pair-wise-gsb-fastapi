"""
Lifecycle tests for FastAPI's "function" / "request" yield-dependency scopes.

The scenario under test: an *auth* dependency borrows a short-lived *signer*
("function" scope, exits before the response is sent), while a streaming
response relies on a longer-lived *audit* context ("request" scope, exits only
after the last chunk). Resources must never be used after their scope exited.

Graphs are declared as data (`GraphSpec`) and interpreted by `build_app()`, so
each graph and its resulting event ledger can be replayed standalone:

    python tests/test_dependency_yield_scope_lifecycle.py auth-signer-audit
    python tests/test_dependency_yield_scope_lifecycle.py auth-signer-audit --disconnect

Every scenario asserts an exact expected event order on the ledger (enter /
borrow / exit / raise per resource), not just the final HTTP status.
"""

import asyncio
import dataclasses
import inspect
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, NamedTuple

import pytest
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.exceptions import DependencyScopeError
from fastapi.responses import StreamingResponse
from fastapi.security import SecurityScopes


class Event(NamedTuple):
    resource: str
    kind: str  # enter | borrow | borrow-closed | exit | raise | stream-cancelled
    detail: str = ""


class Ledger:
    """Ordered in-memory event ledger recording the lifecycle of resources."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self._tokens: dict[str, int] = {}

    def next_token(self, name: str) -> str:
        self._tokens[name] = self._tokens.get(name, 0) + 1
        return f"{name}#{self._tokens[name]}"

    def record(self, resource: str, kind: str, detail: str = "") -> None:
        self.events.append(Event(resource, kind, detail))

    def count(self, resource: str, kind: str) -> int:
        return sum(
            1
            for event in self.events
            if event.resource == resource and event.kind == kind
        )

    def index_after(self, resource: str, kind: str, after: int = -1) -> int:
        for index in range(after + 1, len(self.events)):
            event = self.events[index]
            if event.resource == resource and event.kind == kind:
                return index
        raise AssertionError(
            f"event ({resource!r}, {kind!r}) not found after index {after}\n"
            + self.dump()
        )

    def dump(self) -> str:
        return "\n".join(
            f"{index:03d} {event.resource:<16} {event.kind:<16} {event.detail}"
            for index, event in enumerate(self.events)
        )


class TrackedResource:
    """A resource whose uses ("borrows") are recorded, including uses after exit."""

    def __init__(self, name: str, ledger: Ledger) -> None:
        self.name = name
        self.ledger = ledger
        self.token = ledger.next_token(name)
        self.open = True

    def borrow(self, *, who: str) -> None:
        kind = "borrow" if self.open else "borrow-closed"
        self.ledger.record(self.name, kind, f"{who}:{self.token}")


@dataclass(frozen=True)
class NodeSpec:
    name: str
    raise_on_enter: bool = False
    raise_on_exit: bool = False
    wants_security_scopes: bool = False


@dataclass(frozen=True)
class Edge:
    provider: str
    scope: Literal["function", "request"] | None = "request"
    use_cache: bool = True
    security_scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class GraphSpec:
    name: str
    diagram: str
    nodes: tuple[NodeSpec, ...]
    # consumer node name (or "endpoint") -> providers it depends on
    edges: dict[str, tuple[Edge, ...]]
    response: Literal["json", "stream"] = "json"
    # providers the streaming response body borrows after the endpoint returned
    stream_borrows: tuple[str, ...] = ()
    overrides: dict[str, str] = field(default_factory=dict)


def make_node_fn(node: NodeSpec, ledger: Ledger) -> Any:
    async def node_fn(**kwargs: Any) -> AsyncIterator[TrackedResource]:
        detail = ""
        security_scopes = kwargs.get("security_scopes")
        if isinstance(security_scopes, SecurityScopes):
            detail = "scopes:" + ",".join(security_scopes.scopes)
        ledger.record(node.name, "enter", detail)
        if node.raise_on_enter:
            ledger.record(node.name, "raise", "on-enter")
            raise HTTPException(status_code=503, detail=f"{node.name}-enter-failure")
        resource = TrackedResource(node.name, ledger)
        for value in kwargs.values():
            if isinstance(value, TrackedResource):
                value.borrow(who=node.name)
        try:
            yield resource
        except BaseException as exc:
            ledger.record(node.name, "exit", f"exc:{type(exc).__name__}")
            raise
        else:
            ledger.record(node.name, "exit", "clean")
        finally:
            resource.open = False
        if node.raise_on_exit:
            ledger.record(node.name, "raise", "on-exit")
            raise HTTPException(status_code=503, detail=f"{node.name}-exit-failure")

    node_fn.__name__ = f"dep_{node.name}"
    return node_fn


def _edge_parameter(edge: Edge, provider_fn: Any, index: int) -> inspect.Parameter:
    marker: Any
    if edge.security_scopes:
        # The public Security() has no "scope" parameter; scope=None computes
        # to "request" for generator dependencies.
        marker = Security(
            provider_fn,
            scopes=list(edge.security_scopes),
            use_cache=edge.use_cache,
        )
    else:
        marker = Depends(provider_fn, scope=edge.scope, use_cache=edge.use_cache)
    return inspect.Parameter(
        f"dep_{edge.provider}_{index}",
        inspect.Parameter.KEYWORD_ONLY,
        annotation=Annotated[TrackedResource, marker],
    )


def _edge_pairs(spec: GraphSpec, consumer: str) -> list[tuple[str, Edge]]:
    return [
        (f"dep_{edge.provider}_{index}", edge)
        for index, edge in enumerate(spec.edges.get(consumer, ()))
    ]


def make_endpoint(spec: GraphSpec, ledger: Ledger, gate: asyncio.Event | None) -> Any:
    pairs = _edge_pairs(spec, "endpoint")

    async def endpoint(**kwargs: Any) -> Any:
        for value in kwargs.values():
            if isinstance(value, TrackedResource):
                value.borrow(who="endpoint")
        if spec.response == "json":
            return {"status": "ok"}
        stream_resources = [
            kwargs[param_name]
            for param_name, edge in pairs
            if edge.provider in spec.stream_borrows
        ]

        async def gen() -> AsyncIterator[str]:
            try:
                for index in range(2):
                    if index > 0 and gate is not None:
                        await gate.wait()
                    for resource in stream_resources:
                        resource.borrow(who=f"stream-{index}")
                    yield f"chunk-{index}"
            except asyncio.CancelledError:
                ledger.record("endpoint", "stream-cancelled")
                raise

        return StreamingResponse(gen(), media_type="text/plain")

    endpoint.__name__ = f"endpoint_{spec.name}"
    return endpoint


def build_app(
    spec: GraphSpec, ledger: Ledger, gate: asyncio.Event | None = None
) -> FastAPI:
    """Interpret a GraphSpec into a real FastAPI app (raises on illegal graphs)."""
    app = FastAPI()
    node_fns = {node.name: make_node_fn(node, ledger) for node in spec.nodes}
    for node in spec.nodes:
        params = [
            _edge_parameter(edge, node_fns[edge.provider], index)
            for index, edge in enumerate(spec.edges.get(node.name, ()))
        ]
        if node.wants_security_scopes:
            params.append(
                inspect.Parameter(
                    "security_scopes",
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=SecurityScopes,
                )
            )
        node_fns[node.name].__signature__ = inspect.Signature(params)
    endpoint = make_endpoint(spec, ledger, gate)
    endpoint.__signature__ = inspect.Signature(
        [
            _edge_parameter(edge, node_fns[edge.provider], index)
            for index, edge in enumerate(spec.edges.get("endpoint", ()))
        ]
    )
    app.get("/")(endpoint)
    for original, replacement in spec.overrides.items():
        app.dependency_overrides[node_fns[original]] = node_fns[replacement]
    return app


@dataclass
class ReplayResult:
    spec: GraphSpec
    ledger: Ledger
    status: int | None
    body: bytes


async def replay(spec: GraphSpec, *, disconnect: bool = False) -> ReplayResult:
    """Replay a graph against an in-memory ASGI harness (no server, no sockets).

    With disconnect=True the client goes away after the first streamed chunk:
    the endpoint generator is parked on an event barrier and gets cancelled.
    """
    ledger = Ledger()
    gate = asyncio.Event() if disconnect else None
    app = build_app(spec, ledger, gate=gate)
    scope = {
        "type": "http",
        "asgi": {"spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }
    receive_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    receive_queue.put_nowait({"type": "http.request", "body": b"", "more_body": False})
    sent: list[dict[str, Any]] = []
    disconnected = False

    async def receive() -> dict[str, Any]:
        return await receive_queue.get()

    async def send(message: dict[str, Any]) -> None:
        nonlocal disconnected
        sent.append(message)
        if message["type"] == "http.response.start":
            ledger.record("asgi", "response-start", str(message["status"]))
        elif message["type"] == "http.response.body":
            ledger.record("asgi", "body", f"more:{message.get('more_body', False)}")
            if disconnect and not disconnected:
                disconnected = True
                ledger.record("asgi", "disconnect")
                receive_queue.put_nowait({"type": "http.disconnect"})

    task = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(task, timeout=10)
    status = next(
        (
            message["status"]
            for message in sent
            if message["type"] == "http.response.start"
        ),
        None,
    )
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return ReplayResult(spec=spec, ledger=ledger, status=status, body=body)


# --- Ledger assertion helpers -------------------------------------------------


def assert_event_order(ledger: Ledger, *events: tuple[str, str]) -> None:
    position = -1
    for resource, kind in events:
        position = ledger.index_after(resource, kind, after=position)


def assert_balanced(ledger: Ledger, *names: str) -> None:
    """Every entered resource exits exactly once, unless it failed on enter."""
    for name in names:
        enters = ledger.count(name, "enter")
        enter_failures = sum(
            1
            for event in ledger.events
            if event.resource == name
            and event.kind == "raise"
            and event.detail == "on-enter"
        )
        exits = ledger.count(name, "exit")
        assert exits == enters - enter_failures, (
            f"{name}: {enters} enters, {enter_failures} enter-failures, "
            f"{exits} exits\n{ledger.dump()}"
        )
        if exits:
            assert ledger.index_after(name, "enter") < ledger.index_after(name, "exit")


def assert_no_use_after_exit(ledger: Ledger) -> None:
    violations = [event for event in ledger.events if event.kind == "borrow-closed"]
    assert not violations, f"resource used after its scope exited:\n{ledger.dump()}"


def assert_exits_before_response(ledger: Ledger, *names: str) -> None:
    """Function-scoped resources must exit before the response starts."""
    response_start = ledger.index_after("asgi", "response-start")
    for name in names:
        assert (
            ledger.index_after(name, "exit") < response_start
        ), f"{name} exited after the response started\n{ledger.dump()}"


def assert_enter_exit_count(ledger: Ledger, name: str, expected: int) -> None:
    assert ledger.count(name, "enter") == expected, (
        f"{name}: expected {expected} enters, got "
        f"{ledger.count(name, 'enter')}\n{ledger.dump()}"
    )
    assert ledger.count(name, "exit") == expected, (
        f"{name}: expected {expected} exits, got "
        f"{ledger.count(name, 'exit')}\n{ledger.dump()}"
    )


# --- Graph definitions (replayable artifacts) ---------------------------------

GRAPH_SHARED_CACHE = GraphSpec(
    name="shared-cache",
    diagram="""
    # Legal shared node: one cached "db" borrowed by two consumers.
    # db must be entered/exited exactly once, after the response.
    endpoint -> svc_a -> db (request, cached)
             -> svc_b -> db (same cached node)
    """,
    nodes=(NodeSpec("db"), NodeSpec("svc_a"), NodeSpec("svc_b")),
    edges={
        "svc_a": (Edge("db"),),
        "svc_b": (Edge("db"),),
        "endpoint": (Edge("svc_a"), Edge("svc_b")),
    },
    response="json",
)

GRAPH_AUTH_SIGNER_AUDIT = GraphSpec(
    name="auth-signer-audit",
    diagram="""
    # Auth borrows a temporary signer (both function scope, exit before the
    # response). Auth also legally references a request-scoped directory
    # (function -> request is allowed). The streaming response borrows the
    # longer-lived audit context (request scope).
    endpoint -> auth (function) -> signer (function)
             |                 -> directory (request)
             -> audit (request, borrowed by the stream)
    """,
    nodes=(
        NodeSpec("signer"),
        NodeSpec("directory"),
        NodeSpec("auth"),
        NodeSpec("audit"),
    ),
    edges={
        "auth": (Edge("signer", scope="function"), Edge("directory")),
        "endpoint": (Edge("auth", scope="function"), Edge("audit")),
    },
    response="stream",
    stream_borrows=("audit",),
)

GRAPH_NESTED_SECURITY = GraphSpec(
    name="nested-security",
    diagram="""
    # Nested Security: identity (scopes=["items"]) wraps vault
    # (scopes=["me"]); OAuth scopes must propagate inwards.
    endpoint -> Security(identity, ["items"]) -> Security(vault, ["me"])
    """,
    nodes=(
        NodeSpec("vault", wants_security_scopes=True),
        NodeSpec("identity", wants_security_scopes=True),
    ),
    edges={
        "identity": (Edge("vault", security_scopes=("me",)),),
        "endpoint": (Edge("identity", security_scopes=("items",)),),
    },
    response="json",
)

GRAPH_NO_CACHE = GraphSpec(
    name="no-cache",
    diagram="""
    # use_cache=False: the same provider declared twice is entered and
    # cleaned up twice, producing two distinct resource instances.
    endpoint -> x (cached) + x (use_cache=False)
    """,
    nodes=(NodeSpec("x"),),
    edges={"endpoint": (Edge("x"), Edge("x", use_cache=False))},
    response="json",
)

GRAPH_OVERRIDE = GraphSpec(
    name="override",
    diagram="""
    # Dependency override: the replacement inherits the original's
    # "function" scope, so it must exit before the response starts.
    endpoint -> service (function) [overridden by service_override]
    """,
    nodes=(NodeSpec("service"), NodeSpec("service_override")),
    edges={"endpoint": (Edge("service", scope="function"),)},
    response="stream",
    overrides={"service": "service_override"},
)

GRAPH_AUTH_ENTER_FAILURE = dataclasses.replace(
    GRAPH_AUTH_SIGNER_AUDIT,
    name="auth-enter-failure",
    nodes=(
        NodeSpec("signer"),
        NodeSpec("directory"),
        NodeSpec("auth", raise_on_enter=True),
        NodeSpec("audit"),
    ),
    response="json",
    stream_borrows=(),
)

GRAPH_SIGNER_EXIT_FAILURE = dataclasses.replace(
    GRAPH_AUTH_SIGNER_AUDIT,
    name="signer-exit-failure",
    nodes=(
        NodeSpec("signer", raise_on_exit=True),
        NodeSpec("directory"),
        NodeSpec("auth"),
        NodeSpec("audit"),
    ),
    response="json",
    stream_borrows=(),
)

GRAPH_SPECS: dict[str, GraphSpec] = {
    spec.name: spec
    for spec in (
        GRAPH_SHARED_CACHE,
        GRAPH_AUTH_SIGNER_AUDIT,
        GRAPH_NESTED_SECURITY,
        GRAPH_NO_CACHE,
        GRAPH_OVERRIDE,
        GRAPH_AUTH_ENTER_FAILURE,
        GRAPH_SIGNER_EXIT_FAILURE,
    )
}


# --- Scenario tests -------------------------------------------------------------


@pytest.mark.anyio
async def test_shared_cache_node_cleaned_up_once() -> None:
    result = await replay(GRAPH_SHARED_CACHE)
    ledger = result.ledger
    assert result.status == 200
    assert_event_order(
        ledger,
        ("db", "enter"),
        ("svc_a", "enter"),
        ("svc_b", "enter"),
        ("svc_a", "borrow"),
        ("svc_b", "borrow"),
        ("asgi", "response-start"),
        ("svc_b", "exit"),
        ("svc_a", "exit"),
        ("db", "exit"),
    )
    # Shared cached node: entered and cleaned up exactly once despite two consumers.
    assert_enter_exit_count(ledger, "db", 1)
    # Both consumers borrowed the same cached instance.
    borrows = [
        event.detail
        for event in ledger.events
        if event.resource == "db" and event.kind == "borrow"
    ]
    assert len(borrows) == 2
    assert len({detail.split(":")[1] for detail in borrows}) == 1
    assert_balanced(ledger, "db", "svc_a", "svc_b")
    assert_no_use_after_exit(ledger)


@pytest.mark.anyio
async def test_function_scope_exits_before_stream_request_scope_after() -> None:
    result = await replay(GRAPH_AUTH_SIGNER_AUDIT)
    ledger = result.ledger
    assert result.status == 200
    assert result.body == b"chunk-0chunk-1"
    assert_event_order(
        ledger,
        ("signer", "enter"),
        ("directory", "enter"),
        ("auth", "enter"),
        ("audit", "enter"),
        ("auth", "borrow"),
        ("audit", "borrow"),
        # function scope unwinds (LIFO) before the response is sent
        ("auth", "exit"),
        ("signer", "exit"),
        ("asgi", "response-start"),
        # the stream borrows the still-open request-scoped audit context
        ("audit", "borrow"),
        ("audit", "borrow"),
        # request scope unwinds (LIFO) only after the last chunk
        ("audit", "exit"),
        ("directory", "exit"),
    )
    assert_exits_before_response(ledger, "auth", "signer")
    assert_balanced(ledger, "signer", "directory", "auth", "audit")
    assert_no_use_after_exit(ledger)


@pytest.mark.anyio
async def test_streaming_client_disconnect_still_cleans_up() -> None:
    result = await replay(GRAPH_AUTH_SIGNER_AUDIT, disconnect=True)
    ledger = result.ledger
    assert result.status == 200
    assert result.body == b"chunk-0"
    assert_event_order(
        ledger,
        ("signer", "exit"),
        ("asgi", "response-start"),
        ("audit", "borrow"),
        ("asgi", "disconnect"),
        ("endpoint", "stream-cancelled"),
        ("audit", "exit"),
        ("directory", "exit"),
    )
    # The interrupted stream borrowed the audit context exactly once.
    assert ledger.count("audit", "borrow") == 2  # endpoint + first chunk
    assert_balanced(ledger, "signer", "directory", "auth", "audit")
    assert_no_use_after_exit(ledger)


@pytest.mark.anyio
async def test_nested_security_scopes_propagate_and_clean_up() -> None:
    result = await replay(GRAPH_NESTED_SECURITY)
    ledger = result.ledger
    assert result.status == 200
    enter_events = [event for event in ledger.events if event.kind == "enter"]
    assert ("identity", "enter", "scopes:items") in [
        (e.resource, e.kind, e.detail) for e in enter_events
    ]
    assert ("vault", "enter", "scopes:items,me") in [
        (e.resource, e.kind, e.detail) for e in enter_events
    ]
    assert_event_order(
        ledger,
        ("vault", "enter"),
        ("identity", "enter"),
        ("asgi", "response-start"),
        ("identity", "exit"),
        ("vault", "exit"),
    )
    assert_balanced(ledger, "vault", "identity")
    assert_no_use_after_exit(ledger)


@pytest.mark.anyio
async def test_use_cache_false_enters_and_cleans_up_twice() -> None:
    result = await replay(GRAPH_NO_CACHE)
    ledger = result.ledger
    assert result.status == 200
    assert_enter_exit_count(ledger, "x", 2)
    borrows = [
        event.detail
        for event in ledger.events
        if event.resource == "x" and event.kind == "borrow"
    ]
    assert len(borrows) == 2
    # The cached and the uncached declaration produced distinct instances.
    assert len({detail.split(":")[1] for detail in borrows}) == 2
    assert_event_order(
        ledger,
        ("x", "enter"),
        ("x", "enter"),
        ("asgi", "response-start"),
        ("x", "exit"),
        ("x", "exit"),
    )
    assert_no_use_after_exit(ledger)


@pytest.mark.anyio
async def test_override_inherits_function_scope_timing() -> None:
    result = await replay(GRAPH_OVERRIDE)
    ledger = result.ledger
    assert result.status == 200
    # The original dependency never runs; the override does.
    assert ledger.count("service", "enter") == 0
    assert ledger.count("service_override", "enter") == 1
    # The override inherits the original's "function" scope: it exits before
    # the response starts even though the response is streamed.
    assert_exits_before_response(ledger, "service_override")
    assert_balanced(ledger, "service_override")
    assert_no_use_after_exit(ledger)


@pytest.mark.anyio
async def test_dependency_raise_on_enter_unwinds_entered_resources() -> None:
    result = await replay(GRAPH_AUTH_ENTER_FAILURE)
    ledger = result.ledger
    assert result.status == 503
    assert result.body == b'{"detail":"auth-enter-failure"}'
    assert_event_order(
        ledger,
        ("signer", "enter"),
        ("directory", "enter"),
        ("auth", "enter"),
        ("auth", "raise"),
        # already-entered resources unwind with the error, function scope first
        ("signer", "exit"),
        ("directory", "exit"),
        ("asgi", "response-start"),
    )
    exit_details = {
        event.resource: event.detail for event in ledger.events if event.kind == "exit"
    }
    assert exit_details["signer"] == "exc:HTTPException"
    assert exit_details["directory"] == "exc:HTTPException"
    # Downstream resources are never entered, the endpoint never borrows.
    assert ledger.count("audit", "enter") == 0
    assert not [event for event in ledger.events if event.kind == "borrow"]
    assert_balanced(ledger, "signer", "directory", "auth", "audit")


@pytest.mark.anyio
async def test_raise_after_yield_unwinds_request_scope_with_error() -> None:
    result = await replay(GRAPH_SIGNER_EXIT_FAILURE)
    ledger = result.ledger
    assert result.status == 503
    assert result.body == b'{"detail":"signer-exit-failure"}'
    assert_event_order(
        ledger,
        ("auth", "exit"),
        ("signer", "exit"),
        ("signer", "raise"),
        # request-scoped resources are unwound with the exit-time exception
        ("audit", "exit"),
        ("directory", "exit"),
        ("asgi", "response-start"),
    )
    exit_details = {
        event.resource: event.detail for event in ledger.events if event.kind == "exit"
    }
    assert exit_details["audit"] == "exc:HTTPException"
    assert exit_details["directory"] == "exc:HTTPException"
    # The endpoint's response never started: the only body sent is the 503
    # error response, after the exit-time failure.
    assert ledger.index_after("asgi", "body") > ledger.index_after("signer", "raise")
    assert_balanced(ledger, "signer", "directory", "auth", "audit")


# --- Illegal graphs must fail at definition time --------------------------------

ILLEGAL_REQUEST_USES_FUNCTION = GraphSpec(
    name="illegal-request-uses-function",
    diagram="""
    # Illegal: a request-scoped consumer outlives its function-scoped
    # provider; must fail when the route is registered, not mid-request.
    endpoint -> auth (request) -> signer (function)
    """,
    nodes=(NodeSpec("signer"), NodeSpec("auth")),
    edges={
        "auth": (Edge("signer", scope="function"),),
        "endpoint": (Edge("auth", scope="request"),),
    },
)

ILLEGAL_DEFAULT_SCOPE_USES_FUNCTION = dataclasses.replace(
    ILLEGAL_REQUEST_USES_FUNCTION,
    name="illegal-default-scope-uses-function",
    edges={
        "auth": (Edge("signer", scope="function"),),
        "endpoint": (Edge("auth", scope=None),),
    },
)


def test_request_scope_cannot_depend_on_function_scope() -> None:
    ledger = Ledger()
    with pytest.raises(DependencyScopeError, match="cannot depend on"):
        build_app(ILLEGAL_REQUEST_USES_FUNCTION, ledger)
    # The failure happens at route registration: no resource was touched.
    assert ledger.events == []


def test_default_scope_cannot_depend_on_function_scope() -> None:
    ledger = Ledger()
    with pytest.raises(DependencyScopeError, match="cannot depend on"):
        build_app(ILLEGAL_DEFAULT_SCOPE_USES_FUNCTION, ledger)
    assert ledger.events == []


def test_function_scope_may_depend_on_function_scope() -> None:
    legal = dataclasses.replace(
        ILLEGAL_REQUEST_USES_FUNCTION,
        name="legal-function-uses-function",
        edges={
            "auth": (Edge("signer", scope="function"),),
            "endpoint": (Edge("auth", scope="function"),),
        },
    )
    build_app(legal, Ledger())  # must not raise


# --- Mutation tests: the ledgers must detect inverted cleanup timing ------------


@pytest.mark.anyio
async def test_mutation_inverted_cleanup_timing_is_detected() -> None:
    # Mutation: the temporary signer is cleaned up at request scope (after the
    # response) instead of function scope (before it). The app still returns
    # 200, so only the ledger assertions can catch this.
    mutated = dataclasses.replace(
        GRAPH_AUTH_SIGNER_AUDIT,
        name="auth-signer-audit--mutated",
        edges={
            "auth": (Edge("signer", scope="request"), Edge("directory")),
            "endpoint": (Edge("auth", scope="function"), Edge("audit")),
        },
    )
    result = await replay(mutated)
    assert result.status == 200
    assert_no_use_after_exit(result.ledger)
    with pytest.raises(AssertionError):
        assert_exits_before_response(result.ledger, "auth", "signer")


@pytest.mark.anyio
async def test_mutation_shared_cleanup_count_is_detected() -> None:
    # Mutation: the uncached edge silently becomes cached, so the provider is
    # cleaned up once instead of twice. The response is still 200.
    mutated = dataclasses.replace(
        GRAPH_NO_CACHE,
        name="no-cache--mutated",
        edges={"endpoint": (Edge("x"), Edge("x"))},
    )
    result = await replay(mutated)
    assert result.status == 200
    with pytest.raises(AssertionError):
        assert_enter_exit_count(result.ledger, "x", 2)


if __name__ == "__main__":
    graph_name = sys.argv[1] if len(sys.argv) > 1 else "auth-signer-audit"
    with_disconnect = "--disconnect" in sys.argv
    replay_result = asyncio.run(
        replay(GRAPH_SPECS[graph_name], disconnect=with_disconnect)
    )
    print(f"graph: {replay_result.spec.name}")
    print(replay_result.spec.diagram)
    print(f"status: {replay_result.status} body: {replay_result.body!r}")
    print(replay_result.ledger.dump())
