"""Security-scope aware dependency cache.

A single route can declare two security branches (for example ``read`` and
``admin``) that reuse the same user-loading callable. The per-request
dependency cache may only reuse a result when the *effective security scopes*
are the same set: reusing a weaker ``read`` evaluation as the stronger
``admin`` authorization would be a privilege escalation.

The short dependency graph used below (every node records its call count and
the scopes it actually saw):

``common_db``
    Plain ``Depends``, never touches scopes. Reusable by every branch of the
    same request, never across requests.

``get_user``
    Takes ``SecurityScopes`` directly and enforces them against the
    ``x-scopes`` request header. One cache entry per distinct effective scope
    set; a plain ``Depends`` and a scope-less ``Security`` both resolve to the
    empty scope set and therefore share one entry.

``read_branch`` / ``admin_branch``
    Thin ``Security`` wrappers pinning ``["read"]`` / ``["admin"]`` onto
    ``get_user``. Same callable graph, different effective scopes, so their
    ``get_user`` evaluations must never be shared.

``items_service``
    Plain ``Depends`` wrapper around ``read_branch``: scopes are used
    indirectly through a sub-dependency.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated

import fastapi.dependencies.utils as dep_utils
import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Security
from fastapi.dependencies.models import _get_cache_key
from fastapi.dependencies.utils import get_dependant
from fastapi.security import SecurityScopes
from fastapi.testclient import TestClient


@dataclass
class Recorder:
    calls: dict[str, int] = field(default_factory=dict)
    seen_scopes: dict[str, list[tuple[str, ...]]] = field(default_factory=dict)
    rejections: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = field(
        default_factory=list
    )
    endpoint_runs: int = 0

    def tick(self, name: str, scopes: list[str] | tuple[str, ...]) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1
        self.seen_scopes.setdefault(name, []).append(tuple(scopes))

    def reject(
        self,
        name: str,
        required: list[str] | tuple[str, ...],
        missing: list[str],
    ) -> None:
        self.rejections.append((name, tuple(required), tuple(missing)))

    def reset(self) -> None:
        self.calls.clear()
        self.seen_scopes.clear()
        self.rejections.clear()
        self.endpoint_runs = 0


@dataclass
class SecurityGraph:
    recorder: Recorder
    common_db: Callable[[], str]
    dual_loader: Callable[[], str]
    get_user: Callable[..., dict]
    fake_user: Callable[[SecurityScopes], dict]
    read_branch: Callable[..., dict]
    admin_branch: Callable[..., dict]
    strict_admin_branch: Callable[..., dict]
    items_service: Callable[..., dict]
    gate: Callable[..., dict]


def build_graph() -> SecurityGraph:
    recorder = Recorder()

    def common_db() -> str:
        # No SecurityScopes parameter and not used through Security(): this node
        # is scope-free and cached under the empty scope tuple.
        recorder.tick("common_db", ())
        return "db"

    def dual_loader() -> str:
        # Same callable reused through plain Depends and through Security():
        # cache identity must follow effective scopes, not the callable alone.
        recorder.tick("dual_loader", ())
        return f"dual-{recorder.calls['dual_loader']}"

    def get_user(
        request: Request,
        security_scopes: SecurityScopes,
        db: Annotated[str, Depends(common_db)],
    ) -> dict:
        required = list(security_scopes.scopes)
        recorder.tick("get_user", required)
        granted = [s for s in request.headers.get("x-scopes", "").split(",") if s]
        missing = [scope for scope in required if scope not in granted]
        if missing:
            recorder.reject("get_user", required, missing)
            raise HTTPException(
                status_code=403,
                detail=f"missing scopes {missing}; required {required}",
            )
        return {"checked": tuple(required), "db": db, "granted": tuple(granted)}

    def fake_user(security_scopes: SecurityScopes) -> dict:
        required = list(security_scopes.scopes)
        recorder.tick("fake_user", required)
        return {"checked": tuple(required), "fake": True}

    def read_branch(
        user: Annotated[dict, Security(get_user, scopes=["read"])],
    ) -> dict:
        recorder.tick("read_branch", user["checked"])
        return user

    def admin_branch(
        user: Annotated[dict, Security(get_user, scopes=["admin"])],
    ) -> dict:
        recorder.tick("admin_branch", user["checked"])
        return user

    def strict_admin_branch(
        user: Annotated[dict, Security(get_user, scopes=["admin"])],
    ) -> dict:
        # Defense in depth: re-check the authorization decided inside get_user
        # instead of trusting a cached user object blindly.
        recorder.tick("strict_admin_branch", user["checked"])
        if "admin" not in user["checked"]:
            recorder.reject("strict_admin_branch", ("admin",), ["admin"])
            raise HTTPException(
                status_code=403,
                detail=f"admin branch received {user['checked']!r}",
            )
        return user

    def items_service(
        branch: Annotated[dict, Depends(read_branch)],
    ) -> dict:
        recorder.tick("items_service", branch["checked"])
        return {"service": "items", "user": branch}

    def gate(
        user: Annotated[dict, Security(get_user, scopes=["gate"])],
    ) -> dict:
        recorder.tick("gate", user["checked"])
        return user

    return SecurityGraph(
        recorder=recorder,
        common_db=common_db,
        dual_loader=dual_loader,
        get_user=get_user,
        fake_user=fake_user,
        read_branch=read_branch,
        admin_branch=admin_branch,
        strict_admin_branch=strict_admin_branch,
        items_service=items_service,
        gate=gate,
    )


def build_dual_branch_app(graph: SecurityGraph) -> FastAPI:
    app = FastAPI()

    @app.get("/dual")
    def dual_endpoint(
        read_user: Annotated[dict, Security(graph.read_branch)],
        admin_user: Annotated[dict, Security(graph.admin_branch)],
    ) -> dict:
        graph.recorder.endpoint_runs += 1
        return {"read": read_user, "admin": admin_user}

    @app.get("/dual-strict")
    def dual_strict_endpoint(
        read_user: Annotated[dict, Security(graph.read_branch)],
        admin_user: Annotated[dict, Security(graph.strict_admin_branch)],
    ) -> dict:
        graph.recorder.endpoint_runs += 1
        return {"read": read_user, "admin": admin_user}

    return app


def test_dual_security_branches_keep_scope_results_separate() -> None:
    graph = build_graph()
    client = TestClient(build_dual_branch_app(graph))

    response = client.get("/dual", headers={"x-scopes": "read,admin"})

    assert response.status_code == 200
    assert graph.recorder.endpoint_runs == 1
    payload = response.json()
    assert payload["read"]["checked"] == ["read"]
    assert payload["admin"]["checked"] == ["admin"]
    # Each checker must observe its own scope set; the read result must not be
    # reused as the admin authorization even though get_user is the same call.
    assert graph.recorder.seen_scopes["get_user"] == [("read",), ("admin",)]
    assert graph.recorder.calls["get_user"] == 2
    assert graph.recorder.seen_scopes["read_branch"] == [("read",)]
    assert graph.recorder.seen_scopes["admin_branch"] == [("admin",)]
    # The scope-free common dependency is legitimately shared within the request.
    assert graph.recorder.calls["common_db"] == 1


def test_weaker_scope_is_rejected_under_the_real_cache_key() -> None:
    graph = build_graph()
    client = TestClient(build_dual_branch_app(graph))

    response = client.get("/dual", headers={"x-scopes": "read"})

    assert response.status_code == 403
    assert graph.recorder.endpoint_runs == 0
    # get_user still runs twice: the read branch succeeds, then the admin
    # branch evaluates with its own scopes and records the rejection.
    assert graph.recorder.seen_scopes["get_user"] == [("read",), ("admin",)]
    assert graph.recorder.rejections == [("get_user", ("admin",), ("admin",))]


def test_scope_free_dependency_shared_without_aliasing_security_branch() -> None:
    graph = build_graph()
    app = FastAPI()

    @app.get("/mixed")
    def mixed_endpoint(
        plain_one: Annotated[str, Depends(graph.dual_loader)],
        plain_two: Annotated[str, Depends(graph.dual_loader)],
        secure_no_scope: Annotated[str, Security(graph.dual_loader)],
        secure_read_one: Annotated[str, Security(graph.dual_loader, scopes=["read"])],
        secure_read_two: Annotated[str, Security(graph.dual_loader, scopes=["read"])],
    ) -> dict:
        graph.recorder.endpoint_runs += 1
        return {
            "plain_one": plain_one,
            "plain_two": plain_two,
            "secure_no_scope": secure_no_scope,
            "secure_read_one": secure_read_one,
            "secure_read_two": secure_read_two,
        }

    client = TestClient(app)
    response = client.get("/mixed")

    assert response.status_code == 200
    payload = response.json()
    # Empty-scope evaluations (plain Depends and scope-less Security) share one
    # cache entry; the ["read"] evaluations share a different one.
    assert payload["plain_one"] == payload["plain_two"] == payload["secure_no_scope"]
    assert payload["secure_read_one"] == payload["secure_read_two"]
    assert payload["plain_one"] != payload["secure_read_one"]
    assert graph.recorder.calls["dual_loader"] == 2
    assert graph.recorder.endpoint_runs == 1


def test_same_scope_set_in_different_order_uses_one_cache_entry() -> None:
    graph = build_graph()
    app = FastAPI()

    @app.get("/reordered")
    def reordered_endpoint(
        first: Annotated[dict, Security(graph.get_user, scopes=["read", "admin"])],
        second: Annotated[dict, Security(graph.get_user, scopes=["admin", "read"])],
    ) -> dict:
        graph.recorder.endpoint_runs += 1
        return {"first": first, "second": second}

    client = TestClient(app)
    response = client.get("/reordered", headers={"x-scopes": "read,admin"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["first"] == payload["second"]
    # Order-insensitive equivalence: the scope set is sorted in the cache key.
    assert graph.recorder.calls["get_user"] == 1
    assert graph.recorder.seen_scopes["get_user"] == [("read", "admin")]
    assert graph.recorder.endpoint_runs == 1


def test_sub_dependency_uses_scopes_indirectly_without_propagating_them() -> None:
    graph = build_graph()
    app = FastAPI()

    @app.get("/chain")
    def chain_endpoint(
        items: Annotated[dict, Depends(graph.items_service)],
        admin_user: Annotated[dict, Security(graph.admin_branch)],
    ) -> dict:
        graph.recorder.endpoint_runs += 1
        return {"items": items, "admin": admin_user}

    client = TestClient(app)
    response = client.get("/chain", headers={"x-scopes": "read,admin"})

    assert response.status_code == 200
    assert graph.recorder.endpoint_runs == 1
    # items_service reaches get_user through a plain Depends: read_branch still
    # pins ["read"], and it is a distinct evaluation from the ["admin"] branch.
    assert graph.recorder.seen_scopes["get_user"] == [("read",), ("admin",)]
    assert graph.recorder.calls["get_user"] == 2
    assert graph.recorder.seen_scopes["items_service"] == [("read",)]
    assert graph.recorder.seen_scopes["admin_branch"] == [("admin",)]
    assert graph.recorder.calls["common_db"] == 1


def test_include_router_stacks_nested_security_scopes() -> None:
    graph = build_graph()
    router = APIRouter(dependencies=[Security(graph.gate)])

    @router.get("/nested", dependencies=[Security(graph.get_user, scopes=["read"])])
    def nested_endpoint(
        admin_user: Annotated[dict, Security(graph.admin_branch)],
    ) -> dict:
        graph.recorder.endpoint_runs += 1
        return admin_user

    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)

    response = client.get("/api/nested", headers={"x-scopes": "gate,read,admin"})

    assert response.status_code == 200
    assert graph.recorder.endpoint_runs == 1
    # Router-level gate, route-level read dependency and endpoint-level admin
    # branch accumulate into three distinct effective scope sets for get_user.
    assert graph.recorder.seen_scopes["get_user"] == [
        ("gate",),
        ("read",),
        ("admin",),
    ]
    assert graph.recorder.calls["get_user"] == 3
    assert graph.recorder.seen_scopes["gate"] == [("gate",)]
    assert graph.recorder.seen_scopes["admin_branch"] == [("admin",)]
    assert graph.recorder.calls["common_db"] == 1


def test_use_cache_false_re_evaluates_and_does_not_share_its_result() -> None:
    graph = build_graph()
    app = FastAPI()

    @app.get("/no-cache-admin")
    def no_cache_admin_endpoint(
        read_user: Annotated[dict, Security(graph.read_branch)],
        admin_user: Annotated[dict, Security(graph.admin_branch, use_cache=False)],
    ) -> dict:
        graph.recorder.endpoint_runs += 1
        return {"read": read_user, "admin": admin_user}

    @app.get("/no-cache-then-cached")
    def no_cache_then_cached_endpoint(
        fresh_admin: Annotated[dict, Security(graph.admin_branch, use_cache=False)],
        cached_admin: Annotated[dict, Security(graph.admin_branch)],
    ) -> dict:
        graph.recorder.endpoint_runs += 1
        return {"fresh": fresh_admin, "cached": cached_admin}

    client = TestClient(app)

    response = client.get("/no-cache-admin", headers={"x-scopes": "read,admin"})
    assert response.status_code == 200
    assert graph.recorder.calls["get_user"] == 2
    assert graph.recorder.calls["admin_branch"] == 1
    assert graph.recorder.endpoint_runs == 1

    graph.recorder.reset()
    response = client.get("/no-cache-then-cached", headers={"x-scopes": "admin"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["fresh"] == payload["cached"]
    # The uncached branch writes its solved value, so the later cached branch
    # reuses it instead of executing a second time.
    assert graph.recorder.calls["get_user"] == 1
    assert graph.recorder.calls["admin_branch"] == 1
    assert graph.recorder.endpoint_runs == 1


def test_dependency_override_keeps_scope_entries_distinct() -> None:
    graph = build_graph()
    app = FastAPI()

    @app.get("/overridden")
    def overridden_endpoint(
        read_user: Annotated[dict, Security(graph.read_branch)],
        admin_user: Annotated[dict, Security(graph.admin_branch)],
    ) -> dict:
        graph.recorder.endpoint_runs += 1
        return {"read": read_user, "admin": admin_user}

    app.dependency_overrides[graph.get_user] = graph.fake_user
    client = TestClient(app)

    response = client.get("/overridden", headers={"x-scopes": "read,admin"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["read"] == {"checked": ["read"], "fake": True}
    assert payload["admin"] == {"checked": ["admin"], "fake": True}
    # The override replaces the callable but the scope-part of the cache key is
    # untouched: both scope sets are still evaluated separately.
    assert graph.recorder.calls["fake_user"] == 2
    assert graph.recorder.seen_scopes["fake_user"] == [("read",), ("admin",)]
    assert graph.recorder.calls.get("get_user", 0) == 0
    assert graph.recorder.endpoint_runs == 1


def test_dependency_cache_is_not_shared_across_requests() -> None:
    graph = build_graph()
    client = TestClient(build_dual_branch_app(graph))

    first = client.get("/dual", headers={"x-scopes": "read,admin"})
    assert first.status_code == 200
    assert graph.recorder.calls["get_user"] == 2
    assert graph.recorder.calls["common_db"] == 1

    second = client.get("/dual", headers={"x-scopes": "read,admin"})
    assert second.status_code == 200
    # A fresh per-request cache: every node is solved again, nothing leaks from
    # the first request, and the shared common dep runs once per request.
    assert graph.recorder.calls["get_user"] == 4
    assert graph.recorder.calls["common_db"] == 2
    assert graph.recorder.calls["read_branch"] == 2
    assert graph.recorder.calls["admin_branch"] == 2
    assert graph.recorder.endpoint_runs == 2
    assert first.json() == second.json()


def test_cache_key_encodes_scopes_computed_scope_and_callable() -> None:
    graph = build_graph()

    plain = get_dependant(path="/x", call=graph.dual_loader)
    secured = get_dependant(
        path="/x", call=graph.dual_loader, own_oauth_scopes=["read"]
    )
    order_one = get_dependant(
        path="/x", call=graph.get_user, own_oauth_scopes=["read", "admin"]
    )
    order_two = get_dependant(
        path="/x", call=graph.get_user, own_oauth_scopes=["admin", "read"]
    )
    scope_free = get_dependant(path="/x", call=graph.common_db)

    plain_key = _get_cache_key(dependant=plain)
    secured_key = _get_cache_key(dependant=secured)
    scope_free_key = _get_cache_key(dependant=scope_free)

    # Same callable is not enough: the scope tuple participates in the key.
    assert plain_key[0] is secured_key[0]
    assert plain_key != secured_key
    assert plain_key[1] == ()
    assert secured_key[1] == ("read",)
    # Scope-free nodes are keyed with an empty scope tuple, not a wildcard.
    assert scope_free_key[1] == ()
    # Same set, different order: equivalent cache entries.
    assert _get_cache_key(dependant=order_one) == _get_cache_key(
        dependant=order_two
    )

    def generator_dependency():
        yield "value"

    generator_dependant = get_dependant(path="/x", call=generator_dependency)
    # Generator dependencies carry the computed "request" scope in the key.
    assert _get_cache_key(dependant=generator_dependant)[2] == "request"
    assert plain_key[2] == ""


def test_removing_scopes_from_cache_key_causes_privilege_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = build_graph()
    app = FastAPI()

    @app.get("/admin")
    def admin_endpoint(
        read_user: Annotated[dict, Security(graph.get_user, scopes=["read"])],
        admin_user: Annotated[dict, Security(graph.get_user, scopes=["admin"])],
    ) -> dict:
        graph.recorder.endpoint_runs += 1
        return {"read": read_user, "admin": admin_user}

    client = TestClient(app)

    # Baseline: the real key keeps the two evaluations apart and the request is
    # denied because the token only carries "read".
    denied = client.get("/admin", headers={"x-scopes": "read"})
    assert denied.status_code == 403
    assert graph.recorder.endpoint_runs == 0
    assert graph.recorder.seen_scopes["get_user"] == [("read",), ("admin",)]
    assert graph.recorder.rejections == [("get_user", ("admin",), ("admin",))]

    original_get_cache_key = dep_utils._get_cache_key

    def cache_key_without_scopes(*, dependant, uses_scopes_cache=None):
        call, _scopes, computed_scope = original_get_cache_key(
            dependant=dependant,
            uses_scopes_cache=uses_scopes_cache,
        )
        # Simulate the vulnerable variant of the cache key: only callable
        # identity and computed scope, dropping the effective security scopes.
        return call, (), computed_scope

    monkeypatch.setattr(dep_utils, "_get_cache_key", cache_key_without_scopes)
    graph.recorder.reset()

    bypassed = client.get("/admin", headers={"x-scopes": "read"})

    # This is an actual authorization bypass, not merely a different call count:
    # the endpoint executes and the admin branch receives the read-only user.
    assert bypassed.status_code == 200
    assert graph.recorder.endpoint_runs == 1
    assert graph.recorder.calls["get_user"] == 1
    assert graph.recorder.seen_scopes["get_user"] == [("read",)]
    payload = bypassed.json()
    assert payload["read"]["checked"] == ["read"]
    assert payload["admin"]["checked"] == ["read"]
    assert graph.recorder.rejections == []


def test_mutated_key_still_fails_closed_with_a_strict_checker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = build_graph()
    client = TestClient(build_dual_branch_app(graph))
    original_get_cache_key = dep_utils._get_cache_key

    def cache_key_without_scopes(*, dependant, uses_scopes_cache=None):
        call, _scopes, computed_scope = original_get_cache_key(
            dependant=dependant,
            uses_scopes_cache=uses_scopes_cache,
        )
        return call, (), computed_scope

    monkeypatch.setattr(dep_utils, "_get_cache_key", cache_key_without_scopes)

    response = client.get("/dual-strict", headers={"x-scopes": "read"})

    # get_user is solved once for read and its cached object is handed to the
    # admin branch, but strict_admin_branch re-checks and blocks the request.
    assert response.status_code == 403
    assert graph.recorder.endpoint_runs == 0
    assert graph.recorder.calls["get_user"] == 1
    assert graph.recorder.seen_scopes["get_user"] == [("read",)]
    assert graph.recorder.seen_scopes["strict_admin_branch"] == [("read",)]
    assert graph.recorder.rejections == [
        ("strict_admin_branch", ("admin",), ("admin",))
    ]
