"""C4.5 — the usage projection of a settle agrees with the ledger terminal.

The documents say the usage log "always records the actual spend", and readers use
it that way: the savings report and the reconcile join both price against
`cost_microusd` from this row rather than querying the ledger. That made the row a
second statement about the same money, with nothing checking the two agree — and the
one that a customer-facing number is computed from was the one with no test.

Two separate properties, and only the first is about equality:

  1. When a settle charged the pool, the usage row for that request carries the SAME
     figure the ledger terminal recorded. A projection that disagrees with the charge
     of record is worse than an absent one, because the disagreement is invisible.
  2. A projection that could not be written does not change what the ledger charged.
     The money move completes first and the row is written after it, so an outage on
     the reporting table costs a row and never leaves reserved budget frozen with the
     provider already paid. The failure is raised rather than swallowed, so the
     missing row is loud; what it must not do is reach back into the charge.
"""
from __future__ import annotations

import pytest

from dynamo import CreditLedgerRepository
from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
from dynamo.tenants import TenantsRepository
from dynamo.user_tenants import UserTenantsRepository
from mvp._pipeline import reserve_credit_for_model, settle_reservation_and_log
from mvp.deps import AuthenticatedUser


TENANT = "usage-projection-tenant"
MODEL = "claude-sonnet-5"


def _user(uid: str = "u-proj") -> AuthenticatedUser:
    UserTenantsRepository().ensure(
        user_id=uid, tenant_id=TENANT, role="user", total_credit=10 ** 12)
    return AuthenticatedUser(
        user_id=uid, email="projection@test.example", org_id=TENANT, roles=["user"],
        raw_claims={}, auth_kind="jwt", key_scopes=None, api_key_hash=None,
    )


def _seed() -> str:
    TenantsRepository().create(
        tenant_id=TENANT, name="Usage Projection", team_lead_user_id="admin-owned",
        default_credit=10_000_000, created_by="test")
    period = current_period()
    TenantBudgetsRepository().set_manual_limit(
        tenant_id=TENANT, period=period, manual_limit_microusd=1_000_000_000)
    return period


def _usage_rows(tenant_id: str) -> list[dict]:
    from dynamo.usage_logs import UsageLogsRepository

    repo = UsageLogsRepository()
    return repo._table.query(
        KeyConditionExpression="tenant_id = :t",
        ExpressionAttributeValues={":t": tenant_id},
    ).get("Items", [])


def _settle(user, ctx, *, tokens_in: int, tokens_out: int):
    return settle_reservation_and_log(
        user=user, tenants_repo=ctx.tenants_repo, reservation=2500,
        actual_input_tokens=tokens_in, actual_output_tokens=tokens_out,
        model_id="us.anthropic.claude-sonnet-5", context=ctx,
    )


def test_the_usage_row_carries_the_figure_the_ledger_charged(dynamodb_mock):
    period = _seed()
    user = _user()
    ctx = reserve_credit_for_model(
        user, reservation_tokens=2500, model_name=MODEL,
        input_tokens_est=2000, max_output_tokens=400,
        input_bytes=6000, payload_hash="feedface", extra_input_tokens=0,
    )
    assert ctx.pool_active is True
    _settle(user, ctx, tokens_in=1800, tokens_out=300)

    terminal = CreditLedgerRepository().get_terminal(
        tenant_id=TENANT, period=period, hold_id=ctx.hold_id)
    assert terminal is not None and terminal["event_type"] == "SETTLE"
    charged = int(terminal["settled_delta_microusd"])
    assert charged > 0, "a priced settle that charged nothing would make this vacuous"

    rows = _usage_rows(TENANT)
    assert len(rows) == 1, rows
    assert int(rows[0]["cost_microusd"]) == charged
    # And the pool counter agrees with both, so there is one number in three places
    # rather than three numbers.
    pool = TenantBudgetsRepository().pool_summary(TENANT, period)
    assert int(pool["pool_settled_microusd"]) == charged


def test_a_projection_that_cannot_be_written_does_not_change_the_charge(dynamodb_mock,
                                                                       monkeypatch):
    """The ordering, pinned. The money is complete — pool counters moved, hold gone,
    terminal written — BEFORE the projection is attempted, so a failure on the
    reporting table cannot leave reserved budget frozen with the provider already
    paid.

    What it does NOT do is swallow the failure: the exception reaches the caller, so
    a systematic outage on the usage table is loud rather than a silently thinning
    audit trail. That is a deliberate choice in the opposite direction from the
    observability hook two functions away, which is swallowed — the difference is
    that a hook has no reader and this row has three."""
    import dynamo.usage_logs as usage_logs_mod

    period = _seed()
    user = _user()
    ctx = reserve_credit_for_model(
        user, reservation_tokens=2500, model_name=MODEL,
        input_tokens_est=2000, max_output_tokens=400,
        input_bytes=6000, payload_hash="feedface", extra_input_tokens=0,
    )

    def _boom(self, **kwargs):
        raise RuntimeError("usage-logs unavailable")

    monkeypatch.setattr(usage_logs_mod.UsageLogsRepository, "record", _boom)
    with pytest.raises(RuntimeError):
        _settle(user, ctx, tokens_in=1800, tokens_out=300)

    terminal = CreditLedgerRepository().get_terminal(
        tenant_id=TENANT, period=period, hold_id=ctx.hold_id)
    assert terminal is not None and terminal["event_type"] == "SETTLE"
    charged = int(terminal["settled_delta_microusd"])
    assert charged > 0
    pool = TenantBudgetsRepository().pool_summary(TENANT, period)
    assert int(pool["pool_settled_microusd"]) == charged
    # The reservation is not left frozen: the hold was returned in the same
    # transaction that recorded the charge, one statement before the projection was
    # even attempted.
    assert int(pool["pool_reserved_microusd"]) == 0
    # And no usage row exists, which is the honest state — absent, not a zero.
    assert _usage_rows(TENANT) == []
