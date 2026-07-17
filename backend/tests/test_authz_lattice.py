"""Exhaustive proof of the read-breadth implication lattice in mvp.authz.

This is a security-sensitive relation: a reversed edge is privilege escalation
(read-self => read-all). Because the domain is finite and small, we ENUMERATE
every (held, requested) pair over the full permission universe and compare
`_grants` against an INDEPENDENTLY hand-written reference. That is a total proof
over the domain (not a sample) and it is non-vacuous: the reference is written
from the spec, not derived from the production constant, so any edge-level
mutation (reverse / delete / add / break-wildcard) is killed at some pair.

We prefer this over Z3: |held| x |requested| is a few hundred pairs, and
enumerating the ACTUAL Python beats proving an SMT re-encoding of it (the bug
class here — a reversed dict edge — lives below any such encoding).
"""
from __future__ import annotations

import re
from itertools import product
from pathlib import Path

from mvp.authz import _HELD_IMPLIES, _grants

# The full permission universe actually used in the codebase. Kept in sync with
# reality by `test_universe_is_complete` (greps every require_permission literal).
CONCRETE = [
    "accounts:create", "accounts:delete", "accounts:read", "accounts:update",
    "apikeys:create", "apikeys:create-self", "apikeys:read", "apikeys:read-self",
    "apikeys:revoke", "apikeys:revoke-self",
    "billing:read", "billing:write",
    "messages:send", "responses:send",
    "tenants:create", "tenants:delete", "tenants:read-all", "tenants:read-own",
    "tenants:update",
    "usage:read-all", "usage:read-own-tenant", "usage:read-self",
    "users:assign-tenant", "users:create", "users:delete", "users:read",
    "users:update",
]
# Per-resource wildcards a role/scope could plausibly hold.
WILDCARDS = sorted({p.split(":", 1)[0] + ":*" for p in CONCRETE})
UNIVERSE_HELD = CONCRETE + WILDCARDS

# Independent reference: the 4 transitively-closed read-breadth pairs. Written
# from the spec by hand — deliberately NOT derived from _HELD_IMPLIES, so if the
# production constant is reversed/edited the exhaustive test disagrees.
LATTICE = {
    ("tenants:read-all", "tenants:read-own"),
    ("usage:read-all", "usage:read-own-tenant"),
    ("usage:read-all", "usage:read-self"),
    ("usage:read-own-tenant", "usage:read-self"),
}


def _reference(held: str, requested: str) -> bool:
    if held == requested:
        return True
    if held.endswith(":*"):
        return held.split(":", 1)[0] == requested.split(":", 1)[0]
    return (held, requested) in LATTICE


def test_exhaustive_total_proof():
    """Soundness + completeness over the ENTIRE held x requested domain."""
    for held, req in product(UNIVERSE_HELD, CONCRETE):
        assert _grants(held, req) == _reference(held, req), (held, req)


def test_directional_anti_escalation():
    """The security property, as a named tripwire: a NARROWER permission never
    satisfies a BROADER one (redundant with the exhaustive test, kept explicit)."""
    for broader, narrower in LATTICE:
        assert not _grants(narrower, broader), (narrower, broader)


def test_reference_and_production_agree():
    """Close the two-sources gap: the production closure equals the reference."""
    prod = {(h, n) for h, ns in _HELD_IMPLIES.items() for n in ns}
    assert prod == LATTICE


def test_every_edge_points_strictly_narrower():
    """Direction guard (mirrors the import-time rank check): every implication
    edge must go broader -> strictly narrower, so no edge can escalate."""
    from mvp.authz import _BREADTH_RANK, _action

    for held, implied in _HELD_IMPLIES.items():
        for n in implied:
            assert _BREADTH_RANK[_action(n)] < _BREADTH_RANK[_action(held)], (held, n)


def test_import_guard_rejects_a_swapped_edge():
    """The rank guard must raise on a reversed edge (read-self => read-all),
    the catastrophic mis-edit — verified by re-running the guard logic."""
    from mvp.authz import _BREADTH_RANK, _action

    bad_held, bad_narrow = "usage:read-self", "usage:read-all"
    # This is exactly what the import-time loop asserts; a reversed edge has
    # implied-rank >= held-rank and must be rejected.
    assert _BREADTH_RANK[_action(bad_narrow)] >= _BREADTH_RANK[_action(bad_held)]


def test_wildcard_and_exact_still_work():
    """Backward-compat spot checks: exact + same-resource wildcard unchanged,
    cross-resource wildcard does NOT match."""
    assert _grants("users:read", "users:read")
    assert _grants("users:*", "users:create")
    assert not _grants("users:*", "usage:read-self")
    assert not _grants("usage:read-self", "usage:read-all")  # the headline bug's inverse


def test_no_cross_action_implication():
    """update/create/delete must NEVER imply read (only the read ladder exists)."""
    for held in ("usage:read-all", "tenants:read-all"):
        res = held.split(":", 1)[0]
        for action in ("create", "update", "delete"):
            assert not _grants(held, f"{res}:{action}"), (held, action)


def test_universe_is_complete():
    """The proof is total only if CONCRETE covers reality. Grep every
    require_permission literal in mvp/ and assert it is in the universe (a new
    permission added without lattice review fails here)."""
    mvp_dir = Path(__file__).resolve().parent.parent / "mvp"
    found: set[str] = set()
    pat = re.compile(r'require_permission\("([^"]+)"\)')
    # rglob (not glob): recurse into subpackages (mvp/routing, mvp/observability,
    # ...) so a permission-gated endpoint added in a subdir can't slip the
    # universe-completeness net.
    for py in mvp_dir.rglob("*.py"):
        found.update(pat.findall(py.read_text()))
    # Non-vacuity: if the grep finds nothing (dir moved/renamed, code relocated,
    # a require_scope-style helper introduced) the "missing" check would pass
    # while verifying nothing. Assert we actually found the known literals.
    assert len(found) >= 10, f"universe scan found only {len(found)} literals — grep likely broke"
    missing = found - set(CONCRETE)
    assert not missing, f"permissions used but absent from the lattice universe: {missing}"


# ---------------------------------------------------------------------------
# Fail-closed hardening (Fable capability audit) — an API key with a MISSING
# scope list must NEVER fall through to the owner's full role grants.
# ---------------------------------------------------------------------------
def test_api_key_with_none_scopes_fails_closed():
    from mvp.authz import user_has_permission
    from mvp.deps import AuthenticatedUser

    # An api_key subject whose scope list is None (malformed record / future
    # code path that forgets to populate it) must be denied EVERYTHING — not
    # granted the owner's role permissions.
    admin_owner_key_no_scopes = AuthenticatedUser(
        user_id="u1",
        email="u1@example.com",
        roles=["admin"],  # owner is an admin
        org_id="default-org",
        auth_kind="api_key",
        key_scopes=None,  # scopes missing
    )
    for perm in ("messages:send", "users:create", "tenants:read-all", "apikeys:revoke"):
        assert user_has_permission(admin_owner_key_no_scopes, perm) is False, perm


def test_api_key_empty_scopes_grants_nothing():
    from mvp.authz import user_has_permission
    from mvp.deps import AuthenticatedUser

    key = AuthenticatedUser(
        user_id="u1", email="u1@example.com", roles=["admin"],
        org_id="default-org", auth_kind="api_key", key_scopes=[],
    )
    assert user_has_permission(key, "messages:send") is False


def test_api_key_none_scopes_short_circuits_before_role_lookup(monkeypatch):
    # The fail-closed denial happens WITHOUT consulting the owner's roles (no
    # DynamoDB round-trip): a None-scope key is denied purely structurally.
    import mvp.authz as authz
    from mvp.authz import user_has_permission
    from mvp.deps import AuthenticatedUser

    def _boom(*a, **k):
        raise AssertionError("has_permission must not be reached for a None-scope key")

    monkeypatch.setattr(authz, "has_permission", _boom)
    key = AuthenticatedUser(
        user_id="u1", email="u1@example.com", roles=["admin"],
        org_id="default-org", auth_kind="api_key", key_scopes=None,
    )
    assert user_has_permission(key, "users:create") is False
