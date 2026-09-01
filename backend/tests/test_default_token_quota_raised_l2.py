"""L2 (docs/design/limits.md, PR "money-first"): the per-user token quota is a
loose fairness backstop, not a money ceiling — so its default must be raised
from 100,000 to 10,000,000 tokens, on BOTH paths that can produce it.

The contract's "Verified by" originally named `credit_source == "global_default"`
for a fresh membership. That reading is wrong for this codebase and the contract
was amended (Amendment 3): `TenantsRepository.create` stamps `default_credit`
onto the tenant row, and it must keep doing so, because `TenantItem.default_credit`
is a REQUIRED response field serialised as `int(item.get("default_credit") or 0)` —
a tenant row without the attribute would report a ceiling of **0** to every API
and console reader. So a fresh membership legitimately resolves
`credit_source == "tenant_default"`, and what L2 promises is the NUMBER, on both
paths: the stamped copy, and the global branch a row carrying no `default_credit`
falls through to.

Spec, from the shipped statement in `docs/design/limits.md` and clause C14:

    DEFAULT_TENANT_CREDIT   default 10000000   "The per-user token fairness
    backstop. Raised from 100,000; deliberately loose, because the binding
    ceiling is now the pool."

and the L2 row's "Verified by": "Unit: a fresh user's ceiling is 10,000,000
and `credit_source` is the global default. No arithmetic on the reserve path
changes."

The "no arithmetic on the reserve path changes" half of L2 is NOT re-tested
here: `test_tenant_pool_budget.py`, `test_credit_reservation.py` and the
Z3/stateful billing suites already exercise `reserve()`/`refund()` byte-for-byte
and are green today: this PR must not touch that path at all, so those tests
are the standing evidence for it. Raising a constant makes no arithmetic
change by construction, so there is no id-specific arithmetic behaviour left
for L2 to add a test for — only the two numbers below.
"""
from __future__ import annotations

from dynamo.tenants import TenantsRepository
from dynamo.user_tenants import UserTenantsRepository


def test_fresh_user_default_ceiling_is_ten_million(dynamodb_mock):
    """An unconfigured tenant, created through the ordinary route with nothing
    set by hand, then a brand-new membership on it: the ceiling is 10,000,000.

    The provenance is `tenant_default`, and that is asserted rather than merely
    tolerated: creation stamping `default_credit` is what keeps
    `TenantItem.default_credit` from serialising as 0, so a change that stopped
    stamping it would be a wire regression this test should catch.
    """
    tenants = TenantsRepository()
    tenant = tenants.create(
        name="Fresh Co",
        team_lead_user_id="admin-owned",
        created_by="admin-1",
        # default_credit intentionally omitted — "nothing set by hand".
    )
    tenant_id = tenant["tenant_id"]

    uts = UserTenantsRepository()
    row = uts.ensure(user_id="user-fresh-1", tenant_id=tenant_id, role="user")

    assert int(row["total_credit"]) == 10_000_000
    assert row["credit_source"] == "tenant_default"


def test_default_tenant_credit_env_var_drives_the_backstop(
    dynamodb_mock, monkeypatch
):
    """`DEFAULT_TENANT_CREDIT` must be read live (not baked into a constant at
    import time) so an operator's override actually takes effect — the same
    property `_default_credit_fallback()` already has for the per-tenant
    value. Set an arbitrary override and confirm a fresh membership follows it
    exactly, proving the raised default is env-driven rather than a second
    hardcoded literal that happens to also say 10000000."""
    monkeypatch.setenv("DEFAULT_TENANT_CREDIT", "42000000")

    tenants = TenantsRepository()
    tenant = tenants.create(
        name="Override Co", team_lead_user_id="admin-owned", created_by="admin-1"
    )
    uts = UserTenantsRepository()
    row = uts.ensure(user_id="user-fresh-2", tenant_id=tenant["tenant_id"], role="user")

    assert int(row["total_credit"]) == 42_000_000


def test_a_tenant_row_carrying_no_default_credit_falls_through_to_the_same_number(
    dynamodb_mock, monkeypatch
):
    """The second path to the same number, and the one that used to disagree.

    A tenant row with no `default_credit` attribute — the shape a row written
    before that field existed, or by hand, still has — resolves through
    `_resolve_tenant_default`'s global branch. That branch read a class constant
    bound at import (100,000) while creation read an env-driven function, so
    raising one left the other at the old ceiling: two definitions of one number.
    Both now come from `dynamo.tenants.default_tenant_credit()`, so this asserts
    the value, the provenance, AND that the env var reaches this branch too.
    """
    monkeypatch.setenv("DEFAULT_TENANT_CREDIT", "7000000")

    tenants = TenantsRepository()
    tenant = tenants.create(
        name="Legacy Co", team_lead_user_id="admin-owned", created_by="admin-1"
    )
    tenant_id = tenant["tenant_id"]

    # Strip the stamped attribute to reproduce the legacy row shape.
    tenants._table.update_item(
        Key={"tenant_id": tenant_id},
        UpdateExpression="REMOVE default_credit",
    )

    row = UserTenantsRepository().ensure(
        user_id="user-legacy-1", tenant_id=tenant_id, role="user"
    )

    assert int(row["total_credit"]) == 7_000_000
    assert row["credit_source"] == "global_default"
