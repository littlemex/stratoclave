"""C6.12 sweep: every rate-limited route either authenticates its caller
(bucketed by the verified user, `core.rate_limit._request_key`) or is a named,
unauthenticated sign-in route (bucketed by address plus, for `/login`,
`/respond` and `/sso-exchange`, the identity it names -- `core.rate_limit`'s
`AUTH_SIGN_IN_ADDRESS_SCOPE` / `check_identity_rate_limit`).

`test_rate_limit_xff.py` proves the MECHANISM (a limited route IS bucketed by
`request.state.rate_principal` when one is set) on one synthetic route. That
is not evidence about any REAL route: a route decorated with
`@limiter.limit(...)` but never gated on `get_current_user` would still pass
every assertion in that file. This file is the other half: find every route
actually decorated with `.limit(...)` in `mvp/`, and check each one against
the real, constructed app.
"""
from __future__ import annotations

import ast
import os
import pathlib

import pytest

os.environ.setdefault("ENVIRONMENT", "development")

from fastapi.routing import APIRoute

from main import app
from mvp import deps

MVP = pathlib.Path(__file__).resolve().parents[1] / "mvp"

#: Unauthenticated by design -- the sign-in surface itself (same routes named
#: in `test_write_routes_need_write_permission.py`'s
#: `WRITES_WITHOUT_PERMISSION_GATE`, which is the authority-side statement of
#: the same fact). "module.py:function" -> why it has no authenticating
#: dependency to find.
UNAUTHENTICATED_SIGN_IN_ROUTES: dict[str, str] = {
    "mvp/cognito_auth.py:login": "unauthenticated by design: it is the sign-in",
    "mvp/cognito_auth.py:respond_challenge": "unauthenticated by design: the sign-in challenge",
    "mvp/sso_exchange.py:sso_exchange": "unauthenticated by design: the vouch exchange (C6.8)",
    "mvp/ui_ticket.py:consume_ui_ticket": "single-use ticket, not a bearer credential (C6.6)",
}


def _rate_limited_functions() -> set[str]:
    """{"mvp/<file>.py:<function>"} for every route-decorated function that
    also carries a `.limit(...)` decorator.

    Read from source, not from the live app: `DynamoRateLimiter.limit`'s
    `functools.wraps` makes the wrapper indistinguishable from an undecorated
    handler by introspecting the constructed route alone, and a limit applied
    through `dependencies=[...]` or any other shape this does not recognise
    is a sweep gap worth seeing, not papering over -- see
    `test_every_decorated_route_is_recognised_as_a_route_method_pair` below.
    """
    found: set[str] = set()
    for path in sorted(MVP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(MVP.parent).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorator_attrs = {
                d.func.attr if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                else (d.attr if isinstance(d, ast.Attribute) else None)
                for d in node.decorator_list
            }
            is_route = decorator_attrs & {"get", "post", "put", "patch", "delete", "api_route"}
            is_limited = "limit" in decorator_attrs
            if is_route and is_limited:
                found.add(f"{rel}:{node.name}")
    return found


def _iter_api_routes():
    """Every `APIRoute` reachable from the constructed app, recursing through
    whatever wrapper FastAPI's `include_router` produces -- `app.routes`
    itself holds those wrappers, not the routes, on the FastAPI version this
    repo pins."""
    def walk(routes):
        for r in routes:
            if isinstance(r, APIRoute):
                yield r
            sub = getattr(getattr(r, "original_router", None), "routes", None)
            if sub:
                yield from walk(sub)
            elif getattr(r, "routes", None):
                yield from walk(r.routes)
    yield from walk(app.routes)


def _route_key(route: APIRoute) -> str:
    module = route.endpoint.__module__
    name = route.endpoint.__name__
    return f"mvp/{module.split('.')[-1]}.py:{name}"


def _depends_on_get_current_user(route: APIRoute) -> bool:
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        if dep.call is deps.get_current_user:
            return True
        stack.extend(dep.dependencies)
    return False


def test_the_sweep_finds_more_than_a_handful_of_routes():
    # A guard on the guard: an empty or near-empty result would make every
    # check below pass for free.
    routes = list(_iter_api_routes())
    assert len(routes) > 50
    under_mvp = [r for r in routes if r.endpoint.__module__.startswith("mvp.")]
    assert len(under_mvp) > 50


def test_every_decorated_route_is_recognised_as_a_route_method_pair():
    # Pins the AST matcher against drift: if this stops finding the known
    # rate-limited routes, every assertion below is vacuously satisfied.
    found = _rate_limited_functions()
    for key in (
        "mvp/cognito_auth.py:login",
        "mvp/cognito_auth.py:respond_challenge",
        "mvp/sso_exchange.py:sso_exchange",
        "mvp/ui_ticket.py:consume_ui_ticket",
        "mvp/billing_authorize.py:authorize",
        "mvp/billing_authorize.py:capture",
        "mvp/billing_authorize.py:void",
    ):
        assert key in found, f"{key} is rate-limited in source but the AST sweep missed it"


def test_every_rate_limited_route_authenticates_or_is_a_named_sign_in_route():
    limited = _rate_limited_functions()
    matched: set[str] = set()
    problems: list[str] = []
    for route in _iter_api_routes():
        key = _route_key(route)
        if key not in limited:
            continue
        matched.add(key)
        if _depends_on_get_current_user(route):
            continue
        if key in UNAUTHENTICATED_SIGN_IN_ROUTES:
            continue
        problems.append(
            f"{key} ({route.path}) is rate-limited but neither depends on "
            f"get_current_user nor is listed in UNAUTHENTICATED_SIGN_IN_ROUTES"
        )
    assert not problems, "\n  ".join([""] + problems)
    # And the reverse: a name in the allowlist that no longer matches a real
    # route is an allowlist entry nobody is checking anything about.
    unmatched_allowlist = set(UNAUTHENTICATED_SIGN_IN_ROUTES) - matched
    assert not unmatched_allowlist, (
        f"listed as an unauthenticated sign-in route but never matched a real, "
        f"rate-limited route: {unmatched_allowlist}"
    )
    # Every function the AST sweep found must have matched a real route, or
    # the two checks above never ran against it at all.
    unmatched_source = limited - matched
    assert not unmatched_source, (
        f"found via source but never registered as a route in the constructed "
        f"app: {unmatched_source}"
    )


@pytest.mark.parametrize("key, why", sorted(UNAUTHENTICATED_SIGN_IN_ROUTES.items()))
def test_every_allowlisted_route_is_still_rate_limited(key, why):
    # The allowlist names an exemption FROM the authentication requirement,
    # not from rate limiting altogether; a route quietly dropped from
    # `.limit(...)` while staying in this list would be invisible to the test
    # above (it is not "limited but unauthenticated", it is "not limited").
    assert key in _rate_limited_functions(), (
        f"{key} is in UNAUTHENTICATED_SIGN_IN_ROUTES ({why}) but no longer "
        f"carries a .limit(...) decorator"
    )
