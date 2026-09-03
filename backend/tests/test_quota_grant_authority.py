"""F2 (docs/design/quota-raises.md): R6 + R31 — the two security-critical ids.

R6: Approver authority is a `ConditionCheck` INSIDE the transaction that
    grants money, not a route-level check evaluated earlier and trusted.
R31: That `ConditionCheck` binds the actor to the TENANT READ FROM THE
    REQUEST ROW — never a tenant_id the caller supplies, and never a route
    path parameter (the approve routes take `{request_id}`, not
    `{tenant_id}` — there is no tenant_id in the path to bind to at all).

docs/design/quota-raises.md's exact expressions (R31 row):
  admin actor:      ConditionExpression = "attribute_exists(tenant_id)"
  team_lead actor:   ConditionExpression = "team_lead_user_id = :a"
both against the TENANTS table row keyed by the tenant_id `get_request()`
reports, placed as TransactWriteItems index 0 so `CancellationReasons[0] ==
"ConditionalCheckFailed"` is unambiguously an authority failure.

Section A tests the literal transaction shape directly (no service layer):
build the 3-item TransactWriteItems by hand from `mvp.grants` +
`dynamo.quota_events` builders and execute it against moto, so the proof that
"the transaction's own cancellation" is what refuses — not a prior route
check — cannot be faked by a service function that happens to 403 for some
other, earlier reason. Section B drives `approve_limit_raise` itself.

None of `dynamo.quota_events`, `mvp.grants` exist yet, so every test below
fails today at import.
"""
from __future__ import annotations

from datetime import datetime, timezone

import boto3
import pytest
from botocore.exceptions import ClientError

from dynamo.tenants import TenantsRepository
from tests.quota_events_fixtures import (
    freeze_grants_clock,
    quota_events_table,
    seed_pool_with_grant_fields,
    seed_request,
)

assert quota_events_table  # imported for its pytest-fixture side effect

PERIOD = "2026-09"
# `approve_limit_raise` resolves its target period from `current_period()`
# (real wall clock), never from the frozen `mvp.grants` clock -- so a
# `now_epoch` outside the ACTUAL current month desyncs from the PERIOD these
# tests seed pool rows at, and `latest_permissible_expiry_for_period` then
# refuses every window because `now` already exceeds the (wrong) period's
# end. Mid-month keeps every window comfortably inside R11's bounds.
_MID_PERIOD_EPOCH = int(datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp())


def _table():
    return boto3.resource("dynamodb", region_name="us-east-1").Table(
        "stratoclave-quota-events")


def _seed_two_tenants() -> None:
    tenants = TenantsRepository()
    tenants.create(tenant_id="tenant-a", name="A Co", team_lead_user_id="tl-a", created_by="tl-a")
    tenants.create(tenant_id="tenant-b", name="B Co", team_lead_user_id="tl-b", created_by="tl-b")


def _admin_actor():
    from mvp.deps import AuthenticatedUser

    return AuthenticatedUser(
        user_id="admin-1", email="admin@example.com", org_id="admin-1",
        roles=["admin"], raw_claims={}, auth_kind="cognito",
    )


def _team_lead_actor(user_id: str):
    from mvp.deps import AuthenticatedUser

    return AuthenticatedUser(
        user_id=user_id, email=f"{user_id}@example.com", org_id=user_id,
        roles=["team_lead"], raw_claims={}, auth_kind="cognito",
    )


# ---------------------------------------------------------------------------
# Section A — the literal 3-item transaction, built and executed by hand
# ---------------------------------------------------------------------------

def _build_full_transaction(*, actor, as_owner: bool, tenant_id: str,
                             grant_id: str, request_id: str, amount: int):
    """Assemble exactly the transaction `approve_limit_raise` is specified to
    send: [authority ConditionCheck, grant Put, pool Update].

    `_authority_condition_check_item` takes the full `actor` object plus
    `as_owner` -- the ROUTE'S choice of which check to apply (team-lead
    ownership vs admin), never derived from `actor.roles` (see
    `mvp/grants.py::team_lead_approve_limit_raise`'s own docstring: sniffing
    roles would let a global approver reach a route that claims to enforce
    ownership and have the weaker check applied instead) -- not the bare
    `actor_user_id`/`actor_is_admin` strings docs/design/quota-raises.md's draft used before
    that correction.
    """
    from dynamo.quota_events import QuotaEventsRepository
    from dynamo.tenant_budgets import TenantBudgetsRepository, budget_sk
    from mvp.grants import _authority_condition_check_item

    repo = QuotaEventsRepository()
    condition_check = _authority_condition_check_item(
        actor=actor, as_owner=as_owner, tenant_id=tenant_id,
    )
    grant_put = repo.grant_put_txn_item(
        tenant_id=tenant_id, grant_id=grant_id, request_id=request_id,
        approver_user_id=actor.user_id, approved_amount_microusd=amount,
        expires_at_epoch=2_000_000_000, target_pk=tenant_id,
        target_sk=budget_sk(PERIOD), period=PERIOD,
        created_at="2026-09-02T00:00:00+00:00",
    )
    pool_apply = TenantBudgetsRepository().grant_apply_txn_item(
        target_pk=tenant_id, target_sk=budget_sk(PERIOD),
        approved_amount_microusd=amount, cap_minus_amount=10_000_000 - amount,
    )
    return [condition_check, grant_put, pool_apply]


def test_r31_condition_check_is_transaction_item_zero_and_shaped_per_design(
    dynamodb_mock, quota_events_table,
):
    """Shape check: the authority item IS a `ConditionCheck` (not a `Get` or a
    business-logic pre-check), sits at index 0, and its expression matches
    the implementation's pinned shape for both actor kinds -- the low-level
    DynamoDB wire format (`{"S": ...}` typed values), since this builder feeds
    `boto3.client("dynamodb").transact_write_items` directly rather than the
    resource-API's auto-serialising `Table`."""
    _seed_two_tenants()
    from mvp.grants import _authority_condition_check_item

    admin_actor = _admin_actor()
    admin_check = _authority_condition_check_item(
        actor=admin_actor, as_owner=False, tenant_id="tenant-a")
    assert "ConditionCheck" in admin_check
    assert admin_check["ConditionCheck"]["Key"] == {"tenant_id": {"S": "tenant-a"}}
    assert admin_check["ConditionCheck"]["ConditionExpression"] == "attribute_exists(tenant_id)"

    tl_actor = _team_lead_actor("tl-a")
    tl_check = _authority_condition_check_item(
        actor=tl_actor, as_owner=True, tenant_id="tenant-a")
    assert tl_check["ConditionCheck"]["Key"] == {"tenant_id": {"S": "tenant-a"}}
    assert tl_check["ConditionCheck"]["ConditionExpression"] == "team_lead_user_id = :actor"
    assert tl_check["ConditionCheck"]["ExpressionAttributeValues"] == {":actor": {"S": "tl-a"}}

    txn = _build_full_transaction(
        actor=tl_actor, as_owner=True, tenant_id="tenant-a",
        grant_id="g-shape", request_id="req-shape", amount=1_000,
    )
    assert list(txn[0].keys()) == ["ConditionCheck"], "authority item must be index 0"


def test_r31_team_lead_who_owns_tenant_a_is_refused_on_a_request_for_tenant_b(
    dynamodb_mock, quota_events_table,
):
    """The exact adversarial case R31 names: a permission-holder (team_lead
    role — a FLAT, deployment-global grant in this codebase's RBAC, per
    mvp/authz.py) who legitimately owns tenant-a attempts to approve a
    request whose OWN `tenant_id` attribute says tenant-b. The transaction
    must cancel at the ConditionCheck (index 0); neither the grant Put nor
    the pool Update may land, even though both are perfectly well-formed
    money writes.
    """
    _seed_two_tenants()
    seed_pool_with_grant_fields(
        "tenant-b", PERIOD, pool_limit_microusd=1_000_000,
        pool_granted_microusd=0, grant_cap_microusd=10_000_000,
    )
    txn = _build_full_transaction(
        actor=_team_lead_actor("tl-a"), as_owner=True, tenant_id="tenant-b",
        grant_id="g-cross", request_id="req-cross", amount=5_000,
    )
    client = boto3.client("dynamodb", region_name="us-east-1")
    with pytest.raises(ClientError) as exc:
        client.transact_write_items(TransactItems=txn)
    reasons = [r.get("Code") for r in exc.value.response.get("CancellationReasons", [])]
    assert reasons[0] == "ConditionalCheckFailed", (
        "tl-a owns tenant-a, not tenant-b — refused by the transaction's own "
        "cancellation, at index 0, exactly as R31 requires"
    )

    from dynamo.tenant_budgets import TenantBudgetsRepository
    row = TenantBudgetsRepository().get("tenant-b", PERIOD, consistent_read=True)
    assert int(row["pool_granted_microusd"]) == 0, "no money moved on the cancelled transaction"
    resp = _table().get_item(Key={"pk": "TENANT#tenant-b", "sk": "GRANT#g-cross"})
    assert "Item" not in resp, "no grant row was created on the cancelled transaction"


def test_r31_team_lead_who_owns_tenant_a_may_approve_a_request_for_tenant_a(
    dynamodb_mock, quota_events_table,
):
    """The positive control: the SAME actor, the SAME shape of transaction,
    against their OWN tenant, commits cleanly — proving the refusal above is
    about tenant ownership specifically, not some other broken wiring."""
    _seed_two_tenants()
    seed_pool_with_grant_fields(
        "tenant-a", PERIOD, pool_limit_microusd=1_000_000,
        pool_granted_microusd=0, grant_cap_microusd=10_000_000,
    )
    txn = _build_full_transaction(
        actor=_team_lead_actor("tl-a"), as_owner=True, tenant_id="tenant-a",
        grant_id="g-own", request_id="req-own", amount=5_000,
    )
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.transact_write_items(TransactItems=txn)

    from dynamo.tenant_budgets import TenantBudgetsRepository
    row = TenantBudgetsRepository().get("tenant-a", PERIOD, consistent_read=True)
    assert int(row["pool_granted_microusd"]) == 5_000
    resp = _table().get_item(Key={"pk": "TENANT#tenant-a", "sk": "GRANT#g-own"})
    assert resp["Item"]["status"] == "ACTIVE"


def test_r31_admin_may_approve_any_existing_tenant(dynamodb_mock, quota_events_table):
    _seed_two_tenants()
    seed_pool_with_grant_fields(
        "tenant-b", PERIOD, pool_limit_microusd=1_000_000,
        pool_granted_microusd=0, grant_cap_microusd=10_000_000,
    )
    txn = _build_full_transaction(
        actor=_admin_actor(), as_owner=False, tenant_id="tenant-b",
        grant_id="g-admin", request_id="req-admin", amount=2_000,
    )
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.transact_write_items(TransactItems=txn)  # must not raise

    from dynamo.tenant_budgets import TenantBudgetsRepository
    row = TenantBudgetsRepository().get("tenant-b", PERIOD, consistent_read=True)
    assert int(row["pool_granted_microusd"]) == 2_000


# ---------------------------------------------------------------------------
# Section B — through `approve_limit_raise` itself
# ---------------------------------------------------------------------------

def test_r6_authority_is_read_live_by_the_transaction_not_cached_by_approve(
    dynamodb_mock, quota_events_table, monkeypatch,
):
    """R6: the transaction's ConditionCheck is what decides authority, live,
    on every call — not a value `approve_limit_raise` reads once and trusts
    for the rest of the request, and not the FLAT route-level permission
    (mvp.authz), which this test forces to always say yes so it cannot be
    the thing doing the refusing. Ownership is revoked BETWEEN two calls to
    the same function (the closest a synchronous unit test can get to "mid-
    flight" without a second thread racing the same transaction) and the
    SECOND call must refuse purely on the live Tenants row.
    """
    from mvp import authz, grants

    monkeypatch.setattr(authz, "user_has_permission", lambda user, perm: True)
    _seed_two_tenants()
    seed_pool_with_grant_fields(
        "tenant-a", PERIOD, pool_limit_microusd=1_000_000,
        pool_granted_microusd=0, grant_cap_microusd=10_000_000,
    )
    seed_request(
        _table(), request_id="req-1", tenant_id="tenant-a", user_id="u1",
        asked_amount_microusd=1_000,
    )
    seed_request(
        _table(), request_id="req-2", tenant_id="tenant-a", user_id="u1",
        asked_amount_microusd=1_000,
    )
    actor = _team_lead_actor("tl-a")

    freeze_grants_clock(monkeypatch, _MID_PERIOD_EPOCH)
    # `as_owner=True`: the team-lead ownership form of the authority check
    # (docs/design/quota-raises.md's draft implicitly assumed `approve_limit_raise` derives
    # this from `actor.roles`; the actual route decides it, per
    # `team_lead_approve_limit_raise`'s own docstring, so a direct
    # service-layer call must pass it explicitly).
    grant1 = grants.approve_limit_raise(
        actor=actor, request_id="req-1", approved_amount_microusd=1_000,
        expires_at=_MID_PERIOD_EPOCH + 300, as_owner=True,
    )
    assert grant1["grant"]["status"] == "ACTIVE"

    # Ownership of tenant-a is reassigned away from tl-a. The route-level
    # permission mock above STILL says yes (unconditionally) for every call.
    TenantsRepository().set_owner(tenant_id="tenant-a", new_owner_user_id="tl-new")

    freeze_grants_clock(monkeypatch, _MID_PERIOD_EPOCH + 60)
    with pytest.raises(grants.AuthorityDenied):
        grants.approve_limit_raise(
            actor=actor, request_id="req-2", approved_amount_microusd=1_000,
            expires_at=_MID_PERIOD_EPOCH + 360, as_owner=True,
        )


def test_r31_approve_limit_raise_refuses_team_lead_on_unowned_tenant(
    dynamodb_mock, quota_events_table, monkeypatch,
):
    """Same property as Section A, at the service-function boundary: a
    request whose Request row names tenant-b is refused for an actor who
    owns only tenant-a, with no grant created and no pool moved."""
    from mvp import grants

    _seed_two_tenants()
    seed_pool_with_grant_fields(
        "tenant-b", PERIOD, pool_limit_microusd=1_000_000,
        pool_granted_microusd=0, grant_cap_microusd=10_000_000,
    )
    seed_request(
        _table(), request_id="req-b1", tenant_id="tenant-b", user_id="u-b",
        asked_amount_microusd=1_000,
    )
    freeze_grants_clock(monkeypatch, _MID_PERIOD_EPOCH)
    with pytest.raises(grants.AuthorityDenied):
        grants.approve_limit_raise(
            actor=_team_lead_actor("tl-a"), request_id="req-b1",
            approved_amount_microusd=1_000, expires_at=_MID_PERIOD_EPOCH + 300,
            as_owner=True,
        )

    from dynamo.tenant_budgets import TenantBudgetsRepository
    row = TenantBudgetsRepository().get("tenant-b", PERIOD, consistent_read=True)
    assert int(row["pool_granted_microusd"]) == 0
    resp = _table().get_item(Key={"pk": "REQUEST#req-b1", "sk": "REQUEST"})
    assert resp["Item"]["status"] == "PENDING", "a refused approval must not decide the request"
