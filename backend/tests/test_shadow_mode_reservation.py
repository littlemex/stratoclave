"""Tests for the `shadow` state of the hard-ceiling reservation bound
(docs/design/hard-ceiling.md section 9b) — the rollout-hazard fix.

Background: `dollar_pool_bound_should_gate` returning `_pool_row_exists(tenant_id)`
alone gated a tenant the instant a dollar pool row existed — an unsizeable
image started returning 400, and the reserved amount jumped from the legacy
estimate to a bound several times larger, exhausting pool headroom at
concurrency levels that used to be fine. The contract requires the opposite
order: compute and record the bound, measure what it would refuse on real
traffic, and only then let it gate. `shadow` (a pool row exists, but
`STRATOCLAVE_HARD_CEILING_GATE` is off) is that in-between state:

  - admission reserves the LEGACY estimate, byte-for-byte, so headroom
    exhaustion does not regress versus pre-hard-ceiling behaviour;
  - nothing is refused on the bound;
  - the sound bound is STILL computed and recorded (on
    `ReservationContext.measured_bound_microusd`), because comparing it
    against the actual settle is the entire point of the state.

These tests drive the real decision chain a route takes:
`mvp.reservation_bound.dollar_pool_bound_state` (env-flag-driven) feeds both
the refusal check (`assess_boundability` + state) and `reserve_credit_for_
model`'s `shadow_mode` parameter — not a hand-rolled stand-in for either.
This is the ordinary pytest suite, not the six independently-verified
formal/differential spec files.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
from mvp._pipeline import reserve_credit_for_model, settle_reservation_and_log
from mvp.deps import AuthenticatedUser
from mvp.models import resolve_model
from mvp.pricing import estimate_cost_microusd, rate_for
from mvp.reservation_bound import (
    HARD_CEILING_GATE_ENV,
    ContentSurvey,
    assess_boundability,
    dollar_pool_bound_state,
    strict_reservation_microusd,
)

MODEL_NAME = "claude-sonnet-5"


def _user(seed: dict) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=seed["user_id"],
        email="shadow-mode@test.example",
        org_id=seed["tenant_id"],
        roles=["user"],
        raw_claims={},
        auth_kind="jwt",
        key_scopes=None,
        api_key_hash=None,
    )


def _pricing_key() -> str:
    return resolve_model(MODEL_NAME).pricing_key


def _expected_bound(*, input_bytes: int, max_output_tokens: int) -> int:
    rate = rate_for(_pricing_key())
    return strict_reservation_microusd(
        rate, input_bytes=input_bytes, max_output_tokens=max_output_tokens,
    )


def _expected_legacy(*, input_tokens_est: int, max_output_tokens: int) -> int:
    return estimate_cost_microusd(
        pricing_key=_pricing_key(), input_tokens_est=input_tokens_est,
        max_output_tokens=max_output_tokens,
    )


# ---------------------------------------------------------------------------
# The state itself: a pool row + the gate flag is what flips shadow<->enforced
# ---------------------------------------------------------------------------


def test_pool_row_with_gate_flag_off_is_shadow_not_enforced(
    seed_tenant_with_pool, monkeypatch,
):
    monkeypatch.setenv(HARD_CEILING_GATE_ENV, "0")
    assert dollar_pool_bound_state(seed_tenant_with_pool["tenant_id"]) == "shadow"
    # Unset is the new default: ON, without anyone having to say so.
    monkeypatch.delenv(HARD_CEILING_GATE_ENV, raising=False)
    assert dollar_pool_bound_state(seed_tenant_with_pool["tenant_id"]) == "enforced"


# ---------------------------------------------------------------------------
# The refusal check every route makes: `_boundability.refused and state ==
# "enforced"`. Proven against the REAL `assess_boundability` +
# `dollar_pool_bound_state`, not a re-implementation of the boolean.
# ---------------------------------------------------------------------------


def test_unsizeable_image_is_not_refused_in_shadow_but_is_refused_when_enforced(
    seed_tenant_with_pool, monkeypatch,
):
    tenant_id = seed_tenant_with_pool["tenant_id"]
    unmeasurable_survey = ContentSurvey(text_bytes=100, unmeasurable_images=1)
    boundability = assess_boundability(unmeasurable_survey)
    assert boundability.refused  # sanity: this survey really is unbounded

    monkeypatch.setenv(HARD_CEILING_GATE_ENV, "0")
    shadow_state = dollar_pool_bound_state(tenant_id)
    assert shadow_state == "shadow"
    # This is the EXACT expression every route (`mvp.anthropic`,
    # `mvp.chat_completions`, `mvp.openai_responses`) evaluates before
    # raising 400 `unbounded_content`.
    assert not (boundability.refused and shadow_state == "enforced")

    # Unset is the new default: ON.
    monkeypatch.delenv(HARD_CEILING_GATE_ENV, raising=False)
    enforced_state = dollar_pool_bound_state(tenant_id)
    assert enforced_state == "enforced"
    assert boundability.refused and enforced_state == "enforced"


# ---------------------------------------------------------------------------
# The reservation-amount hazard itself.
# ---------------------------------------------------------------------------


def test_shadow_reserves_the_legacy_estimate_and_still_records_the_bound(
    seed_tenant_with_pool, monkeypatch,
):
    monkeypatch.setenv(HARD_CEILING_GATE_ENV, "0")
    seed = seed_tenant_with_pool
    user = _user(seed)
    input_bytes = 20_000  # deliberately large: the bound must dwarf the estimate
    max_output_tokens = 500
    input_tokens_est = input_bytes // 3

    state = dollar_pool_bound_state(seed["tenant_id"])
    assert state == "shadow"

    ctx = reserve_credit_for_model(
        user, reservation_tokens=input_tokens_est + 1024,
        model_name=MODEL_NAME, input_tokens_est=input_tokens_est,
        max_output_tokens=max_output_tokens,
        input_bytes=input_bytes, payload_hash="shadow" * 8,
        extra_input_tokens=0,
        shadow_mode=(state == "shadow"),
    )

    legacy = _expected_legacy(
        input_tokens_est=input_tokens_est, max_output_tokens=max_output_tokens,
    )
    bound = _expected_bound(input_bytes=input_bytes, max_output_tokens=max_output_tokens)
    assert bound > legacy  # sanity: this input really does exercise the divergence

    # The RESERVED amount — what actually gates the pool — is the legacy
    # number, byte-for-byte, exactly as if the hard-ceiling bound had never
    # shipped. This is the hazard fix: NOT the (larger) sound bound.
    assert ctx.pool_reserved_microusd == legacy
    pool_row = TenantBudgetsRepository().get(seed["tenant_id"], current_period())
    assert int(pool_row["pool_reserved_microusd"]) == legacy

    # The sound bound is STILL computed and recorded — comparing it against
    # the actual settle is the entire purpose of `shadow`.
    assert ctx.measured_bound_microusd == bound
    assert ctx.measured_bound_microusd != ctx.pool_reserved_microusd

    # Ledger recomputability (docs/design/hard-ceiling.md item 4): `bound_mode`
    # records which strategy produced `pool_reserved_microusd`, which was the
    # LEGACY heuristic here, not the bound — a "strict" tag would tell a
    # reader to recompute via `strict_reservation_microusd`, which would NOT
    # reproduce `pool_reserved_microusd` in shadow mode.
    assert ctx.bound_mode is None

    captured: dict = {}
    import dynamo.usage_logs as usage_logs_mod

    real_repo_cls = usage_logs_mod.UsageLogsRepository
    orig_record = real_repo_cls.record

    def _spy_record(self, **kwargs):
        captured.update(kwargs)
        return orig_record(self, **kwargs)

    real_repo_cls.record = _spy_record
    try:
        settle_reservation_and_log(
            user=user, tenants_repo=ctx.tenants_repo,
            reservation=input_tokens_est + 1024,
            actual_input_tokens=input_bytes,
            actual_output_tokens=400,
            model_id="us.anthropic.claude-sonnet-5",
            context=ctx,
        )
    finally:
        real_repo_cls.record = orig_record

    # The pool hold is fully released (the legacy amount, not the bound).
    pool_after = TenantBudgetsRepository().get(seed["tenant_id"], current_period())
    assert int(pool_after["pool_reserved_microusd"]) == 0

    # Both numbers are independently readable per request from the SAME usage
    # row: the actual settled cost, and the recorded (larger) bound — this is
    # what "both must be readable per request" requires.
    assert captured.get("measured_bound_microusd") == bound
    assert captured.get("cost_microusd") != bound


def test_enforced_still_reserves_the_bound_unchanged(seed_tenant_with_pool, monkeypatch):
    """With the gate flag on, all three flip back: refuses on the bound,
    reserves the bound (not the legacy estimate), and the recorded bound
    equals the reserved amount — exactly today's `enforced` behaviour."""
    monkeypatch.setenv(HARD_CEILING_GATE_ENV, "1")
    seed = seed_tenant_with_pool
    user = _user(seed)
    input_bytes = 20_000
    max_output_tokens = 500
    input_tokens_est = input_bytes // 3

    state = dollar_pool_bound_state(seed["tenant_id"])
    assert state == "enforced"

    ctx = reserve_credit_for_model(
        user, reservation_tokens=input_tokens_est + 1024,
        model_name=MODEL_NAME, input_tokens_est=input_tokens_est,
        max_output_tokens=max_output_tokens,
        input_bytes=input_bytes, payload_hash="enforced" * 8,
        extra_input_tokens=0,
        shadow_mode=(state == "shadow"),
    )

    bound = _expected_bound(input_bytes=input_bytes, max_output_tokens=max_output_tokens)
    assert ctx.pool_reserved_microusd == bound
    assert ctx.measured_bound_microusd == bound
    assert ctx.bound_mode == "strict"

    pool_row = TenantBudgetsRepository().get(seed["tenant_id"], current_period())
    assert int(pool_row["pool_reserved_microusd"]) == bound


def test_shadow_admits_a_request_enforced_would_refuse_for_headroom(
    seed_tenant_with_pool, monkeypatch,
):
    """The concrete hazard from the background section: a bound several times
    the legacy estimate can exhaust headroom (or exceed pool_limit outright)
    at a concurrency level the legacy estimate fit comfortably. Same request,
    same tiny pool: `enforced` refuses it outright (`request_does_not_fit_
    pool_limit`); `shadow` admits it because it reserves the legacy number."""
    seed = seed_tenant_with_pool
    tenant_id = seed["tenant_id"]
    input_bytes = 20_000
    max_output_tokens = 500
    input_tokens_est = input_bytes // 3
    legacy = _expected_legacy(
        input_tokens_est=input_tokens_est, max_output_tokens=max_output_tokens,
    )
    bound = _expected_bound(input_bytes=input_bytes, max_output_tokens=max_output_tokens)
    # A pool limit that fits the legacy estimate comfortably but is smaller
    # than the bound — exactly the concurrency level "that used to be fine".
    tiny_limit = legacy + (legacy // 2)
    assert tiny_limit < bound  # sanity: the bound alone cannot fit this pool
    TenantBudgetsRepository().set_pool_limit(
        tenant_id=tenant_id, period=current_period(), pool_limit_microusd=tiny_limit,
    )

    monkeypatch.setenv(HARD_CEILING_GATE_ENV, "1")
    enforced_user = _user(seed)
    with pytest.raises(HTTPException) as exc_info:
        reserve_credit_for_model(
            enforced_user, reservation_tokens=input_tokens_est + 1024,
            model_name=MODEL_NAME, input_tokens_est=input_tokens_est,
            max_output_tokens=max_output_tokens,
            input_bytes=input_bytes, payload_hash="enforced-fit" * 4,
            extra_input_tokens=0, shadow_mode=False,
        )
    assert exc_info.value.status_code == 402
    assert exc_info.value.detail["reason"] == "request_does_not_fit_pool_limit"

    monkeypatch.setenv(HARD_CEILING_GATE_ENV, "0")
    shadow_user = _user(seed)
    ctx = reserve_credit_for_model(
        shadow_user, reservation_tokens=input_tokens_est + 1024,
        model_name=MODEL_NAME, input_tokens_est=input_tokens_est,
        max_output_tokens=max_output_tokens,
        input_bytes=input_bytes, payload_hash="shadow-fit" * 4,
        extra_input_tokens=0, shadow_mode=True,
    )
    assert ctx.pool_reserved_microusd == legacy
    assert ctx.measured_bound_microusd == bound
