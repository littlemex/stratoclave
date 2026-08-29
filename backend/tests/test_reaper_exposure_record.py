"""The reclaim must preserve what it was about, because it deletes the evidence.

`CONTRACT-charge-loss.md`. The reaper credits an expired hold back with
`actual=0`, which asserts the provider charged nothing. Measured on real Bedrock,
that assertion is false for a request that died after its bytes left: a call
abandoned at a 2 s client read timeout was billed 1,493 output tokens.

Holding those reservations instead of returning them needs a durable sweep cursor,
pool-incarnation fencing and a settle-dispatcher branch — so the size of the
problem should be measured before that is built. These tests pin the measurement:
the reclaim's own transaction copies the hold's facts into the RECLAIM terminal
before the Delete destroys them, and only a hold that backed a provider call
counts as exposure.

They also pin what must NOT change: the money. A reclaim moves exactly the
counters it moved before.
"""
from __future__ import annotations

import json
import time

from mvp import _pipeline
from mvp._pipeline import reserve_credit


class _User:
    def __init__(self, user_id, org_id):
        self.user_id = user_id
        self.org_id = org_id
        self.email = "u@example.com"
        self.roles = ("user",)


def _seed(tenant_id):
    from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
    from dynamo.user_tenants import UserTenantsRepository

    period = current_period()
    user = _User(f"user-{tenant_id}", tenant_id)
    UserTenantsRepository().ensure(
        user_id=user.user_id, tenant_id=tenant_id, role="user",
        total_credit=1_000_000_000,
    )
    TenantBudgetsRepository().set_pool_limit(
        tenant_id=tenant_id, period=period, pool_limit_microusd=10_000_000_000,
    )
    return user, period


def _budgets():
    from dynamo.tenant_budgets import TenantBudgetsRepository

    return TenantBudgetsRepository()


def _expire_and_sweep(ctx, tenant_id, period, *, patch=None):
    """Rewrite the hold into the past (the SK embeds the expiry) and sweep it.

    `patch` overrides attributes on the hold row, which is how a hold written by
    an external authorization — or by code that predates an attribute — is
    simulated without reaching into two different reserve paths.
    """
    from dynamo.tenant_budgets import hold_sk as _hsk

    budgets = _budgets()
    item = budgets._table.get_item(
        Key={"tenant_id": tenant_id, "sk": ctx.hold_sk}
    ).get("Item")
    assert item is not None, "the reserve should have written a hold"
    past = int(time.time()) - 10_000
    budgets._table.delete_item(Key={"tenant_id": tenant_id, "sk": ctx.hold_sk})
    item["sk"] = _hsk(period, past, ctx.hold_id)
    item["expires_at"] = past
    for key, val in (patch or {}).items():
        if val is None:
            item.pop(key, None)
        else:
            item[key] = val
    budgets._table.put_item(Item=item)
    _pipeline._sweep_expired_holds(budgets, tenant_id, period)


def _terminal(tenant_id, period, hold_id):
    from dynamo import CreditLedgerRepository

    return CreditLedgerRepository().get_terminal(
        tenant_id=tenant_id, period=period, hold_id=hold_id
    )


def _reserve(user, amount=250_000):
    ctx = reserve_credit(user, 4000, pricing_key="opus", cost_microusd=amount)
    assert ctx.hold_id, "a pooled reserve should hold"
    return ctx


def test_reclaim_preserves_the_holds_facts_past_its_own_delete(dynamodb_mock):
    user, period = _seed("acme-reap-facts")
    ctx = _reserve(user)

    _expire_and_sweep(ctx, user.org_id, period)

    term = _terminal(user.org_id, period, ctx.hold_id)
    assert term and term["event_type"] == "RECLAIM"
    facts = json.loads(term["reaped_hold"])
    assert facts["source"] == "inline", (
        "an inline hold backed a provider call, and that is the only reason its "
        "reclaim is exposure at all"
    )
    assert facts["amount_microusd"] == 250_000
    assert facts["created_at"] and facts["expires_at"]

    holds = _budgets().list_holds(tenant_id=user.org_id, period=period)
    assert not [h for h in holds if h.get("hold_id") == ctx.hold_id], (
        "the hold row is gone, which is why the facts had to be copied"
    )


def test_an_external_hold_is_not_exposure(dynamodb_mock):
    """It never made a provider call, so returning its reservation is correct."""
    user, period = _seed("acme-reap-external")
    ctx = _reserve(user)

    _expire_and_sweep(ctx, user.org_id, period, patch={"source": "external"})

    facts = json.loads(_terminal(user.org_id, period, ctx.hold_id)["reaped_hold"])
    assert facts["source"] == "external"


def test_a_hold_with_no_source_records_none_rather_than_guessing(dynamodb_mock):
    """A row written before `source` existed must not be counted either way."""
    user, period = _seed("acme-reap-legacy")
    ctx = _reserve(user)

    _expire_and_sweep(ctx, user.org_id, period, patch={"source": None})

    facts = json.loads(_terminal(user.org_id, period, ctx.hold_id)["reaped_hold"])
    assert "source" not in facts
    assert facts["amount_microusd"] == 250_000


def test_the_pre_invoke_marker_survives_when_present(dynamodb_mock):
    """The one fact that separates a real leak from a harmless orphan.

    A request that died BEFORE the provider call cost nothing; one that died after
    may have been billed in full. Nothing can reconstruct which, later, so the
    marker has to be carried through the reclaim.
    """
    user, period = _seed("acme-reap-marker")
    ctx = _reserve(user)

    _expire_and_sweep(
        ctx, user.org_id, period,
        patch={"provider_invoked_at": "2026-08-29T00:00:00Z"},
    )

    facts = json.loads(_terminal(user.org_id, period, ctx.hold_id)["reaped_hold"])
    assert facts["provider_invoked_at"] == "2026-08-29T00:00:00Z"


def test_the_reclaim_still_moves_exactly_the_money_it_moved_before(dynamodb_mock):
    """The measurement is not allowed to change the accounting.

    `headroom == limit - reserved - settled` is the row invariant every reader and
    proof reconstructs. Recording facts on the terminal must leave it exactly where
    an unrecorded reclaim would.
    """
    user, period = _seed("acme-reap-money")
    before = _budgets().pool_summary(user.org_id, period)
    ctx = _reserve(user)

    _expire_and_sweep(ctx, user.org_id, period)

    after = _budgets().pool_summary(user.org_id, period)
    assert int(after["pool_reserved_microusd"]) == int(before["pool_reserved_microusd"])
    assert int(after["pool_settled_microusd"]) == int(before["pool_settled_microusd"])
    assert int(after["pool_headroom_microusd"]) == int(before["pool_headroom_microusd"])
    limit = int(after["pool_limit_microusd"])
    assert int(after["pool_headroom_microusd"]) == (
        limit - int(after["pool_reserved_microusd"]) - int(after["pool_settled_microusd"])
    )
