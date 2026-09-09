"""HANDOFF-PR4 I7/P4.6: the admission condition's granted clause.

    (attribute_not_exists(granted_microusd) AND :granted_read = :zero)
      OR granted_microusd >= :granted_read

pins the value the admission path already read (when it built `ceiling =
base + granted`) against whatever the row holds RIGHT NOW, at commit. The two
tests below are the reason the comparison is `>=` and not `=`, in I7's own
words: "a revocation between the read and the write trips it; an approval in
that window does not."

  * a concurrent REVOKE landing between the read and the commit must cancel
    the admission (the row now holds LESS than what was priced in);
  * a concurrent APPROVAL in the same window must NOT cancel it (the row
    holds AT LEAST as much as what was priced in).

A test that only covered the revoke direction would pass just as well against
an implementation that pinned EQUALITY (`granted_microusd = :granted_read`),
which also refuses the concurrent-approval case -- refusing a member at the
exact moment their raise landed, the failure I7 calls out by name. Both halves
are required to actually distinguish `>=` from `=`.

Signature note (report this back to the integrator, the same convention
`test_user_dollar_quota_builder.py` already established for this module): I7
gives the condition's own TEXT but, unlike I5's revoke builder, does not spell
out the reserve builder's new parameter name for the granted value it reads
and pins. Rather than guess a keyword this file cannot verify against
anything, both tests below go through the REAL admission path
(`mvp._pipeline.reserve_credit`) -- the same one
`test_user_dollar_quota_admission_and_refusal.py` already exercises for this
wall -- and inject the "concurrent" write through a SEPARATE client, in the
window between the pipeline building the transaction (which has already
baked in whatever it read) and that transaction actually committing. No
internal parameter name of any kind is assumed anywhere in this file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

import boto3
import pytest
from fastapi import HTTPException

pytest.importorskip("moto")

from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
from dynamo.tenants import TenantsRepository
from dynamo.user_tenants import UserTenantsRepository
from mvp import _pipeline

TENANT = "pr4-granted-race-tenant"
MODEL = "claude-sonnet-5"
DEFAULT_TOKENS = 2500
_UQ_TABLE = os.getenv("DYNAMODB_MODEL_QUOTAS_TABLE", "stratoclave-model-quotas")


@dataclass
class _User:
    user_id: str
    org_id: str
    email: str = "u@example.com"


def _seed_tenant(tenant_id: str, *, pool_limit: int, uq_default_microusd: int) -> str:
    """The same composition `test_user_dollar_quota_admission_and_refusal.py`
    already uses to bring up a tenant with both a pool ceiling and a sealed
    per-user default -- reproduced locally rather than imported, since that
    file's helper is private to it."""
    TenantsRepository().create(
        tenant_id=tenant_id, name=tenant_id, team_lead_user_id=f"admin-{tenant_id}",
        default_credit=10**12, created_by="test")
    period = current_period()
    TenantBudgetsRepository().set_manual_limit(
        tenant_id=tenant_id, period=period, manual_limit_microusd=pool_limit)
    TenantsRepository()._table.update_item(
        Key={"tenant_id": tenant_id},
        UpdateExpression="SET user_dollar_defaults = :m, user_dollar_defaults_version = :v",
        ExpressionAttributeValues={
            ":m": {"2020-01": Decimal(uq_default_microusd)},
            ":v": 1,
        },
    )
    return period


def _user(tenant_id: str, uid: str) -> _User:
    UserTenantsRepository().ensure(user_id=uid, tenant_id=tenant_id, role="user",
                                    total_credit=10**12)
    return _User(user_id=uid, org_id=tenant_id)


def _seed_uq_granted(tenant_id: str, user_id: str, period: str, granted_microusd: int) -> None:
    from mvp.routing.user_dollar_quota import uq_pk, uq_sk

    table = boto3.resource("dynamodb", region_name="us-east-1").Table(_UQ_TABLE)
    table.put_item(Item={
        "pk": uq_pk(tenant_id, user_id), "sk": uq_sk(period),
        "granted_microusd": Decimal(granted_microusd),
    })


def _mutate_granted(tenant_id: str, user_id: str, period: str, delta: int) -> None:
    """The "concurrent" write: made through a SEPARATE low-level client, and
    invoked from inside `_RaceInjectingClient` right before the admission's
    OWN `TransactWriteItems` call is forwarded to the real one -- simulating
    a revoke or an approval landing in the exact window between the read
    that built the admission's items and the commit that evaluates the
    condition against whatever the row holds by then."""
    from mvp.routing.user_dollar_quota import uq_pk, uq_sk

    client = boto3.client("dynamodb", region_name="us-east-1")
    client.update_item(
        TableName=_UQ_TABLE,
        Key={"pk": {"S": uq_pk(tenant_id, user_id)}, "sk": {"S": uq_sk(period)}},
        UpdateExpression="ADD granted_microusd :d",
        ExpressionAttributeValues={":d": {"N": str(int(delta))}},
    )


def _get_uq_row(tenant_id: str, user_id: str, period: str):
    from mvp.routing.user_dollar_quota import uq_pk, uq_sk

    table = boto3.resource("dynamodb", region_name="us-east-1").Table(_UQ_TABLE)
    resp = table.get_item(Key={"pk": uq_pk(tenant_id, user_id), "sk": uq_sk(period)})
    return resp.get("Item")


class _RaceInjectingClient:
    """Forwards every call to the REAL low-level client, but on the FIRST
    `transact_write_items` call, runs `mutate_fn()` first -- the injected
    race -- so the transaction the pipeline already built (with whatever it
    read baked into its condition) commits against a row this test changed
    out from under it, moments before commit rather than moments after."""

    def __init__(self, real, mutate_fn):
        self._real = real
        self._mutate_fn = mutate_fn
        self._injected = False

    def transact_write_items(self, **kwargs):
        if not self._injected and "TransactItems" in kwargs:
            self._injected = True
            self._mutate_fn()
        return self._real.transact_write_items(**kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_a_concurrent_revoke_between_the_read_and_the_commit_cancels_admission(
    dynamodb_mock, monkeypatch,
):
    period = _seed_tenant(TENANT, pool_limit=10**9, uq_default_microusd=10_000_000)
    user_id = "u-revoke-race"
    user = _user(TENANT, user_id)
    # base 10M alone already covers the 6M request below -- deliberately, so
    # this test's refusal can ONLY come from the granted-pin clause itself,
    # never from ordinary headroom insufficiency. I7's own clause is
    # unconditional: it refuses whenever the LIVE granted value has fallen
    # below what was read, regardless of whether the request would still fit
    # under the resulting ceiling. Seeded on TOP of the 10M base so the
    # admission's read sees a 12M ceiling either way.
    _seed_uq_granted(TENANT, user_id, period, 2_000_000)

    real = _pipeline._low_level_client()
    injecting = _RaceInjectingClient(
        real, lambda: _mutate_granted(TENANT, user_id, period, -2_000_000))
    monkeypatch.setattr(_pipeline, "_low_level_client", lambda: injecting)

    # Against `origin/main`, which does not read `granted_microusd` at all,
    # this request is simply admitted (10M base >= 6M) and this whole block
    # raises nothing -- that IS this test's failure at baseline, and it is
    # the right one: nothing here is a typo, the clause this test pins
    # simply does not exist yet.
    with pytest.raises(HTTPException) as ei:
        _pipeline.reserve_credit(
            user, DEFAULT_TOKENS, pricing_key=None, cost_microusd=6_000_000,
            selected_model=MODEL,
        )
    assert 400 <= ei.value.status_code < 500, (
        f"a concurrent revoke landing between the read and the commit must "
        f"refuse the admission as a genuine client-side refusal, not a "
        f"500; got {ei.value.status_code} {ei.value.detail!r}"
    )

    row = _get_uq_row(TENANT, user_id, period)
    assert int((row or {}).get("used", 0)) == 0, (
        "the admission's whole transaction must have been cancelled -- "
        "`used` must NOT have been incremented by a request the granted "
        "clause was supposed to refuse. A partial commit here would be "
        "worse than the refusal itself"
    )
    assert int((row or {}).get("granted_microusd", 0)) == 0, (
        "sanity check on the harness itself: the injected concurrent "
        "revoke must actually have landed, or this test proves nothing "
        "about the clause at all"
    )


def test_a_concurrent_approval_in_the_same_window_does_not_cancel_admission(
    dynamodb_mock, monkeypatch,
):
    """The half that actually distinguishes `>=` from `=`: a member's own
    ceiling RISING between the read and the commit must never be read as a
    reason to refuse them.

    Note for the integrator: unlike the revoke-direction test above, THIS
    half cannot fail against `origin/main` on its own -- baseline never
    reads `granted_microusd` at all, so it already admits this request for
    an unrelated reason (10M base >= 6M) regardless of the race. Its value
    is against a WRONG implementation that pins EQUALITY
    (`granted_microusd = :granted_read`) rather than `>=`: such an
    implementation would refuse this admission where a correct one must
    not, and this is the one assertion in this file that would catch it."""
    period = _seed_tenant(TENANT, pool_limit=10**9, uq_default_microusd=10_000_000)
    user_id = "u-approval-race"
    user = _user(TENANT, user_id)
    _seed_uq_granted(TENANT, user_id, period, 2_000_000)  # ceiling read as 12M

    real = _pipeline._low_level_client()
    injecting = _RaceInjectingClient(
        real, lambda: _mutate_granted(TENANT, user_id, period, 1_000_000))
    monkeypatch.setattr(_pipeline, "_low_level_client", lambda: injecting)

    ctx = _pipeline.reserve_credit(
        user, DEFAULT_TOKENS, pricing_key=None, cost_microusd=6_000_000,
        selected_model=MODEL,
    )
    assert ctx is not None, (
        "a concurrent APPROVAL raising granted_microusd from 2M (what the "
        "admission read) to 3M (what is live at commit) must NOT cancel an "
        "admission the read already justified. Refusing a member at the "
        "exact moment their raise landed is the failure I7 names by name, "
        "and is exactly what an EQUALITY clause (granted_microusd = "
        ":granted_read) would do instead of >="
    )

    row = _get_uq_row(TENANT, user_id, period)
    assert int((row or {}).get("used", 0)) == 6_000_000, (
        "the admission must have actually COMMITTED, not merely avoided "
        "raising -- `used` must carry the full requested amount"
    )
    assert int((row or {}).get("granted_microusd", 0)) == 3_000_000, (
        "sanity check on the harness itself: the injected concurrent "
        "approval must have landed and must not have been clobbered by the "
        "admission's own write, which never touches granted_microusd"
    )
