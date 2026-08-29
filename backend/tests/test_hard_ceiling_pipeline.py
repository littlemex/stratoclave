"""Pipeline-level integration tests for the hard-ceiling reservation bound
(docs/design/hard-ceiling.md), against real (moto) DynamoDB.

Scope: the strict-mode-only first slice (contract section 12). These drive
`reserve_credit_for_model` / `settle_reservation_and_log` directly (the same
seam `tests/test_pipeline_shared.py` uses), reading results back from the
pool row and the credit ledger — not from the code that produced them — to
match the contract's own acceptance-criteria framing ("verified by driving
requests through the gateway and reading both numbers back from the ledger").
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException

from dynamo.tenant_budgets import TenantBudgetsRepository
from dynamo.tenants import TenantsRepository
from mvp._pipeline import reserve_credit_for_model, settle_reservation_and_log
from mvp.deps import AuthenticatedUser
from mvp.models import resolve_model
from mvp.pricing import rate_for
from mvp.reservation_bound import strict_reservation_microusd

MODEL_NAME = "claude-sonnet-5"
TENANT_ID = "hard-ceiling-test-tenant"


def _user(uid: str = "u1", tid: str = TENANT_ID) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=uid,
        email="hardceiling@test.example",
        org_id=tid,
        roles=["user"],
        raw_claims={},
        auth_kind="jwt",
        key_scopes=None,
        api_key_hash=None,
    )


def _seed_tenant_with_pool(pool_limit_microusd: int, bound_mode: str = "strict") -> None:
    TenantsRepository().create(
        tenant_id=TENANT_ID,
        name="Hard Ceiling Test Tenant",
        team_lead_user_id="admin-owned",
        default_credit=10_000_000,
        created_by="test",
    )
    TenantsRepository().update(tenant_id=TENANT_ID, bound_mode=bound_mode)
    TenantBudgetsRepository().set_pool_limit(
        tenant_id=TENANT_ID,
        period=_current_period(),
        pool_limit_microusd=pool_limit_microusd,
    )


def _current_period() -> str:
    from dynamo.tenant_budgets import current_period

    return current_period()


def _pricing_key() -> str:
    return resolve_model(MODEL_NAME).pricing_key


def _expected_strict_bound(*, input_bytes: int, max_output_tokens: int, extra_input_tokens: int = 0) -> int:
    rate = rate_for(_pricing_key())
    return strict_reservation_microusd(
        rate, input_bytes=input_bytes, max_output_tokens=max_output_tokens,
        extra_input_tokens=extra_input_tokens,
    )


# ---------------------------------------------------------------------------
# Acceptance criterion 1 / 5: reserved == the sound bound, and the terminal
# event carries enough to recompute both reservation and charge.
# ---------------------------------------------------------------------------


def test_strict_mode_reserves_the_sound_bound_not_the_legacy_heuristic(dynamodb_mock):
    _seed_tenant_with_pool(pool_limit_microusd=1_000_000_000)
    user = _user()
    input_bytes = 12_000  # deliberately not a number from the contract

    ctx = reserve_credit_for_model(
        user, reservation_tokens=input_bytes // 3 + 1024,
        model_name=MODEL_NAME, input_tokens_est=input_bytes // 3,
        max_output_tokens=500,
        input_bytes=input_bytes, payload_hash="deadbeef" * 8,
        extra_input_tokens=0,
    )
    try:
        assert ctx.pool_active
        assert ctx.bound_mode == "strict"
        expected = _expected_strict_bound(input_bytes=input_bytes, max_output_tokens=500)
        assert ctx.pool_reserved_microusd == expected
        # Sanity: the sound bound must be strictly larger than what the OLD
        # char-count-heuristic would have reserved for the same request (it
        # prices 3 legs, not 4, and estimates rather than bounds tokens).
        from mvp.pricing import estimate_cost_microusd

        legacy = estimate_cost_microusd(
            pricing_key=_pricing_key(), input_tokens_est=input_bytes // 3,
            max_output_tokens=500,
        )
        assert expected > legacy

        # The pool row's own counters agree with what reserve_credit_for_model
        # returned (read back from the ledger side of the system, not from
        # the Python object).
        pool = TenantBudgetsRepository().get(TENANT_ID, _current_period())
        assert int(pool["pool_reserved_microusd"]) == expected

        # Settle at (or under) the bound: overrun must be exactly zero
        # (acceptance criterion 6).
        settle_reservation_and_log(
            user=user, tenants_repo=ctx.tenants_repo,
            reservation=input_bytes // 3 + 1024,
            actual_input_tokens=input_bytes,  # <= the byte bound
            actual_output_tokens=400,  # <= max_output_tokens
            model_id="us.anthropic.claude-sonnet-5",
            context=ctx,
        )
        pool_after = TenantBudgetsRepository().get(TENANT_ID, _current_period())
        assert int(pool_after["pool_reserved_microusd"]) == 0
        assert int(pool_after["pool_settled_microusd"]) <= expected

        # Acceptance criterion 5: the terminal event carries the bound's
        # inputs + overrun fields, recomputable from recorded values alone.
        term = _read_terminal(ctx.hold_id)
        assert term is not None
        assert int(term["reserved_microusd"]) == expected
        assert int(term["overrun_microusd"]) == 0
        assert term["bound_mode"] == "strict"
        import json

        estimate_inputs = json.loads(term["estimate_inputs"])
        assert estimate_inputs["input_bytes"] == input_bytes
        assert estimate_inputs["max_output_tokens"] == 500
    finally:
        pass


def _read_terminal(hold_id: str) -> dict | None:
    from dynamo import CreditLedgerRepository

    return CreditLedgerRepository().get_terminal(
        tenant_id=TENANT_ID, period=_current_period(), hold_id=hold_id,
    )


# ---------------------------------------------------------------------------
# Section 6 / acceptance criterion 2: a request whose bound exceeds the WHOLE
# pool_limit is refused with a reason distinct from ordinary exhaustion.
# ---------------------------------------------------------------------------


def test_bound_exceeding_pool_limit_is_refused_distinctly_from_exhaustion(dynamodb_mock):
    tiny_limit = 100  # micro-USD; any real request's bound dwarfs this
    _seed_tenant_with_pool(pool_limit_microusd=tiny_limit)
    user = _user()

    with pytest.raises(HTTPException) as exc_info:
        reserve_credit_for_model(
            user, reservation_tokens=2000,
            model_name=MODEL_NAME, input_tokens_est=500, max_output_tokens=500,
            input_bytes=5000, payload_hash="abc123",
            extra_input_tokens=0,
        )
    assert exc_info.value.status_code == 402
    assert exc_info.value.detail["reason"] == "request_does_not_fit_pool_limit"
    assert exc_info.value.detail["reason"] != "tenant_pool_exhausted"


# ---------------------------------------------------------------------------
# Section 7a/7b: budget enforcement is opt-in, checked against REAL Dynamo.
# ---------------------------------------------------------------------------


def test_enforcement_active_iff_pool_row_exists(dynamodb_mock, monkeypatch):
    from mvp.reservation_bound import (
        HARD_CEILING_GATE_ENV,
        dollar_pool_bound_should_compute,
        dollar_pool_bound_should_gate,
    )

    no_pool_tenant = "accounting-only-tenant"
    TenantsRepository().create(
        tenant_id=no_pool_tenant, name="Accounting Only",
        team_lead_user_id="admin-owned", default_credit=10_000_000,
        created_by="test",
    )
    assert dollar_pool_bound_should_compute(no_pool_tenant) is False
    assert dollar_pool_bound_should_gate(no_pool_tenant) is False

    _seed_tenant_with_pool(pool_limit_microusd=1_000_000_000)
    assert dollar_pool_bound_should_compute(TENANT_ID) is True
    # A pool row alone is the `shadow` state, not `enforced`
    # (docs/design/hard-ceiling.md section 9b's rollout requirement — see
    # reservation_bound.py's own module docstring): `should_gate` also needs
    # the gate env flag on. Explicitly clear it first so this assertion does
    # not depend on the ambient test-environment default.
    monkeypatch.delenv(HARD_CEILING_GATE_ENV, raising=False)
    assert dollar_pool_bound_should_gate(TENANT_ID) is False
    monkeypatch.setenv(HARD_CEILING_GATE_ENV, "1")
    assert dollar_pool_bound_should_gate(TENANT_ID) is True


def test_pure_accounting_tenant_sees_no_refusal_and_no_pool_debit(dynamodb_mock):
    """Section 7a: a tenant with no pool row must see NO new refusal from
    this change, and the legacy per-user-token path must be exactly what
    runs — `input_bytes=None` is what `dollar_pool_bound_enforcement_active`
    being False causes the route to pass (verified at the route level via
    `test_reservation_bound.py`'s enforcement tests); here we confirm THIS
    chokepoint's behaviour for that same input is unchanged from pre-change
    legacy pricing, and touches no pool item at all."""
    no_pool_tenant = "accounting-only-tenant-2"
    TenantsRepository().create(
        tenant_id=no_pool_tenant, name="Accounting Only 2",
        team_lead_user_id="admin-owned", default_credit=10_000_000,
        created_by="test",
    )
    user = _user(uid="u2", tid=no_pool_tenant)

    ctx = reserve_credit_for_model(
        user, reservation_tokens=2000,
        model_name=MODEL_NAME, input_tokens_est=500, max_output_tokens=500,
        # input_bytes=None: what the route passes when enforcement is off.
    )
    assert ctx.pool_active is False
    assert ctx.bound_mode is None
    # No pool row was ever created for this tenant — confirms the pool item
    # was never touched (the whole point of the opt-in gate).
    assert TenantBudgetsRepository().get(no_pool_tenant, _current_period()) is None


# ---------------------------------------------------------------------------
# Coordinator ITEM 2: the `measured` bound's destination is the per-request
# usage row, never the ledger / a synthesised pool row.
# ---------------------------------------------------------------------------


def test_measured_bound_lands_on_the_usage_row_not_the_ledger(dynamodb_mock):
    """The `measured` state: no pool row for this tenant, but the caller
    (mirroring the route's `dollar_pool_bound_should_compute`, which the
    measurement flag makes True even with no pool) still supplies
    `input_bytes`, so the sound bound IS computed and must be recorded
    somewhere. It must land on `UsageLogsRepository`'s per-request row
    (`measured_bound_microusd`), and must NOT create a pool row, a hold, or
    any ledger event — this tenant has no pool at all."""
    from dynamo import CreditLedgerRepository, UsageLogsRepository

    measured_tenant = "measured-only-tenant"
    TenantsRepository().create(
        tenant_id=measured_tenant, name="Measured Only",
        team_lead_user_id="admin-owned", default_credit=10_000_000,
        created_by="test",
    )
    user = _user(uid="u3", tid=measured_tenant)
    input_bytes = 6000

    ctx = reserve_credit_for_model(
        user, reservation_tokens=2500,
        model_name=MODEL_NAME, input_tokens_est=2000, max_output_tokens=400,
        input_bytes=input_bytes, payload_hash="cafebabe",
        extra_input_tokens=0,
    )
    assert ctx.pool_active is False
    assert ctx.bound_mode == "strict"
    expected = _expected_strict_bound(input_bytes=input_bytes, max_output_tokens=400)
    assert ctx.measured_bound_microusd == expected
    # No pool row exists for this tenant at all (opt-in enforcement: computing
    # the bound must not itself create anything to enforce it against).
    assert TenantBudgetsRepository().get(measured_tenant, _current_period()) is None

    captured: dict = {}
    orig_record = UsageLogsRepository.record

    def _spy_record(self, **kwargs):
        captured.update(kwargs)
        return orig_record(self, **kwargs)

    import dynamo.usage_logs as usage_logs_mod

    real_repo_cls = usage_logs_mod.UsageLogsRepository
    orig_method = real_repo_cls.record
    real_repo_cls.record = _spy_record
    try:
        settle_reservation_and_log(
            user=user, tenants_repo=ctx.tenants_repo,
            reservation=2500,
            actual_input_tokens=input_bytes,
            actual_output_tokens=300,
            model_id="us.anthropic.claude-sonnet-5",
            context=ctx,
        )
    finally:
        real_repo_cls.record = orig_method

    assert captured.get("measured_bound_microusd") == expected
    # Still no pool row, and no ledger partition was ever created for this
    # tenant/period — settle for a poolless context skips the whole
    # `_settle_pool_side` block entirely (pool_active is False).
    assert TenantBudgetsRepository().get(measured_tenant, _current_period()) is None
    ledger_terminal = CreditLedgerRepository().get_terminal(
        tenant_id=measured_tenant, period=_current_period(), hold_id=ctx.hold_id or "none",
    )
    assert ledger_terminal is None


# ---------------------------------------------------------------------------
# Section 8 (rate change between reserve and settle): the bound and the
# settle-time charge must price at the IDENTICAL frozen snapshot.
# ---------------------------------------------------------------------------


def test_bound_and_settle_share_the_identical_frozen_rate_snapshot(dynamodb_mock, monkeypatch):
    _seed_tenant_with_pool(pool_limit_microusd=1_000_000_000)
    user = _user()
    input_bytes = 4000

    ctx = reserve_credit_for_model(
        user, reservation_tokens=2000,
        model_name=MODEL_NAME, input_tokens_est=1000, max_output_tokens=300,
        input_bytes=input_bytes, payload_hash="feedface",
        extra_input_tokens=0,
    )
    assert ctx.rate_snapshot is not None
    frozen = ctx.rate_snapshot

    # Simulate a rate document change AFTER reserve, BEFORE settle: any live
    # read from here on must be ignored by settle (it charges from the frozen
    # snapshot, never re-reading the table).
    import mvp.pricing as pricing_mod

    monkeypatch.setattr(
        pricing_mod, "rate_for",
        lambda pk, repo=None: pricing_mod.Rate(999_000_000, 999_000_000, 999_000_000, 999_000_000),
    )

    settle_reservation_and_log(
        user=user, tenants_repo=ctx.tenants_repo,
        reservation=2000,
        actual_input_tokens=input_bytes,
        actual_output_tokens=200,
        model_id="us.anthropic.claude-sonnet-5",
        context=ctx,
    )
    term = _read_terminal(ctx.hold_id)
    assert term["pricing_version"] == frozen.version
    assert term["reserve_pricing_version"] == frozen.version
    # The settled amount must be computed from the FROZEN rate (small), not
    # the "changed" live rate (which would be enormous).
    from dynamo.credit_ledger import _json_compact  # noqa: F401 (import used only to fail loudly if module shape changes)

    assert int(term["settled_delta_microusd"]) < 999_000_000
