"""C6.9: a route that changes state is gated on a write, or is named with its reason.

`authz.py` states the rule beside `tenants:update-own`: "a write gated on a read
permission is a lie the permission table tells every future reader of it." The
pool-budget route was moved onto the write permission, and `PATCH
/team-lead/tenants/{id}` beside it was not: the rule was written down and nothing
compared it with the routes. This file is that comparison.

Scope and the stand-ins it uses, stated so the clause does not claim more:

- "changes state" is "the route's method is POST, PUT, PATCH or DELETE", over every
  route module under `backend/mvp`.
- A permission's action (the part after the colon) is classified by the explicit
  `READ_ACTIONS` / `WRITE_ACTIONS` sets, which together are exactly the actions in
  `authz.ALL_SCOPES`. A new action fails until a person classifies it.
- Every gate must be in `authz.ALL_SCOPES`, so a typo'd permission cannot pass as
  "not a read".
- A gate is read from `param = Depends(require_permission("..."))`, the form the
  routes use. A `require_permission` call in any other place on a route, a
  `dependencies=[...]` that holds one, or a route registered with `add_api_route`
  is reported as unreadable rather than skipped.
- A write route that decides its authority inside the handler is listed in
  `HANDLER_GATED` with the permission it passes, and that permission is read from
  the handler's call and checked the same way.
- A write route with no permission gate at all is listed in
  `WRITES_WITHOUT_PERMISSION_GATE` with where its authority comes from.
"""
from __future__ import annotations

import ast
import pathlib

from mvp.authz import ALL_SCOPES

MVP = pathlib.Path(__file__).resolve().parents[1] / "mvp"
WRITE_METHODS = {"post", "put", "patch", "delete"}
ROUTE_METHODS = WRITE_METHODS | {"get", "api_route"}

READ_ACTIONS = {"discover", "read", "read-all", "read-own", "read-own-tenant", "read-self"}
WRITE_ACTIONS = {
    "approve", "approve-own", "assign-tenant", "create", "create-self", "delete",
    "grant", "promote", "raise-self", "revoke", "revoke-self", "send", "update",
    "update-own", "update-own-tenant", "write",
}

# Routes that call a gate helper inside the handler: "mvp/<file>.py:<function>" ->
# (helper name, the permission the route must pass it).
HANDLER_GATED: dict[str, tuple[str, str]] = {
    "mvp/admin_vsr_config.py:put_vsr_config": ("_authorize", "tenants:update"),
    "mvp/admin_vsr_config.py:delete_vsr_config": ("_authorize", "tenants:update"),
    "mvp/admin_vsr_config.py:validate_vsr_config": ("_authorize", "tenants:update"),
}

# Routes with no permission gate: "mvp/<file>.py:<function>" -> where the authority
# comes from, including for a caller holding a narrowed API key.
WRITES_WITHOUT_PERMISSION_GATE: dict[str, str] = {
    "mvp/cognito_auth.py:login": "unauthenticated by design: it is the sign-in",
    "mvp/cognito_auth.py:respond_challenge": "unauthenticated by design: the sign-in challenge",
    "mvp/sso_exchange.py:sso_exchange": "unauthenticated by design: the vouch exchange (C6.8)",
    "mvp/me.py:update_me":
        "sets only the caller's own UI locale; no target parameter, no authority changes",
    "mvp/ui_ticket.py:mint_ui_ticket":
        "binds a Cognito access token from the body, verified against JWKS and required to "
        "belong to the caller; the session it hands over carries that token's authority, "
        "not the presenting credential's, so a key cannot widen itself through it",
    "mvp/ui_ticket.py:consume_ui_ticket": "single-use ticket (C6.6)",
}


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _modules():
    for path in sorted(MVP.rglob("*.py")):
        yield path.relative_to(MVP.parent).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def _routes():
    """(key, function, is_write) for every decorated route under mvp/."""
    for rel, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                decs = [d for d in node.decorator_list if isinstance(d, ast.Call)
                        and isinstance(d.func, ast.Attribute) and d.func.attr in ROUTE_METHODS]
                if decs:
                    is_write = any(d.func.attr in WRITE_METHODS for d in decs)
                    yield f"{rel}:{node.name}", node, is_write


def _holds_require_permission(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Call) and _name(n.func) == "require_permission"
               for n in ast.walk(node))


def _gates(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[list[str], list[str]]:
    """(permissions read from `param = Depends(require_permission("..."))`,
    reasons a gate on this route could not be read)."""
    readable: list[str] = []
    unreadable: list[str] = []
    seen: set[int] = set()
    for default in list(fn.args.defaults) + list(fn.args.kw_defaults):
        if isinstance(default, ast.Call) and _name(default.func) == "Depends" and default.args:
            inner = default.args[0]
            if isinstance(inner, ast.Call) and _name(inner.func) == "require_permission":
                seen.add(id(inner))
                arg = inner.args[0] if len(inner.args) == 1 and not inner.keywords else None
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    readable.append(arg.value)
                else:
                    unreadable.append("require_permission without one literal argument")
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and _name(node.func) == "require_permission" \
                and id(node) not in seen:
            unreadable.append(f"require_permission outside a parameter default (line {node.lineno})")
    for dec in fn.decorator_list:
        if isinstance(dec, ast.Call):
            if _name(dec.func) == "api_route":
                unreadable.append("api_route: its methods are not read by this sweep")
    return readable, unreadable


def _handler_permissions(fn: ast.AST, helper: str) -> list[str]:
    found = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and _name(node.func) == helper:
            for kw in node.keywords:
                if kw.arg == "permission" and isinstance(kw.value, ast.Constant):
                    found.append(kw.value.value)
    return found


def _action(permission: str) -> str:
    return permission.split(":", 1)[1]


def test_every_gate_is_in_a_form_the_sweep_reads():
    problems = []
    for key, fn, _ in _routes():
        problems += [f"{key} (line {fn.lineno}): {u}" for u in _gates(fn)[1]]
    for rel, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _name(node.func) in {"add_api_route", "add_route"}:
                problems.append(f"{rel} (line {node.lineno}): a route registered without a decorator")
            if isinstance(node, ast.Call) and any(
                    kw.arg == "dependencies" and _holds_require_permission(kw.value)
                    for kw in node.keywords):
                problems.append(f"{rel} (line {node.lineno}): require_permission in dependencies=[...]")
    assert problems == [], (
        "write these gates as `param = Depends(require_permission(\"<literal>\"))` so C6.9 "
        f"can read them: {problems}")


def test_every_write_route_names_a_write_or_is_listed():
    offenders = []
    for key, fn, is_write in _routes():
        if not is_write:
            continue
        gates = _gates(fn)[0]
        if key in HANDLER_GATED:
            helper, permission = HANDLER_GATED[key]
            gates = gates + _handler_permissions(fn, helper)
            if permission not in gates:
                offenders.append(f"{key}: HANDLER_GATED says {helper}(permission={permission!r}), "
                                 f"the handler passes {gates}")
                continue
        if not gates:
            if key not in WRITES_WITHOUT_PERMISSION_GATE:
                offenders.append(f"{key} (line {fn.lineno}): no permission gate")
            continue
        if all(_action(g) in READ_ACTIONS for g in gates):
            offenders.append(f"{key} (line {fn.lineno}): only read gates {gates}")
    assert offenders == [], (
        "gate these routes on a write permission, or list an ungated one in "
        f"WRITES_WITHOUT_PERMISSION_GATE with where its authority comes from: {offenders}")


def test_every_gate_is_a_known_scope_and_the_actions_are_classified():
    used = set()
    for key, fn, _ in _routes():
        used |= set(_gates(fn)[0])
        if key in HANDLER_GATED:
            used |= set(_handler_permissions(fn, HANDLER_GATED[key][0]))
    assert used <= set(ALL_SCOPES), f"gates not in authz.ALL_SCOPES: {sorted(used - set(ALL_SCOPES))}"
    assert not READ_ACTIONS & WRITE_ACTIONS
    actions = {_action(s) for s in ALL_SCOPES}
    assert READ_ACTIONS | WRITE_ACTIONS == actions, (
        "READ_ACTIONS and WRITE_ACTIONS must be exactly the actions in authz.ALL_SCOPES; "
        f"unclassified {sorted(actions - READ_ACTIONS - WRITE_ACTIONS)}, "
        f"unused {sorted(READ_ACTIONS | WRITE_ACTIONS - actions)}")


def test_the_lists_name_write_routes_that_exist():
    """An entry for a route that was renamed or removed would silently cover the
    next route given that name, and an ungated entry for a route that has since
    gained a gate is stale."""
    writes = {key: fn for key, fn, is_write in _routes() if is_write}
    for listed in (HANDLER_GATED, WRITES_WITHOUT_PERMISSION_GATE):
        assert set(listed) <= set(writes), set(listed) - set(writes)
    assert not set(HANDLER_GATED) & set(WRITES_WITHOUT_PERMISSION_GATE)
    stale = [k for k in WRITES_WITHOUT_PERMISSION_GATE if _gates(writes[k])[0]]
    assert stale == [], stale


def test_the_sweep_reads_the_gates_it_is_about():
    """If the gates changed shape the sweep would read nothing and pass; pin what
    it reads on known routes."""
    by_key = {key: fn for key, fn, _ in _routes()}
    expected = {
        "mvp/team_lead.py:update_own_tenant": ["tenants:update-own"],
        "mvp/team_lead.py:set_own_pool_budget": ["tenants:update-own"],
        "mvp/team_lead.py:create_tenant": ["tenants:create"],
    }
    for key, gates in expected.items():
        assert _gates(by_key[key]) == (gates, []), key
    assert _handler_permissions(by_key["mvp/admin_vsr_config.py:put_vsr_config"], "_authorize") \
        == ["tenants:update"]
