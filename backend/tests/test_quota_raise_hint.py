"""F3 / R36 — the `raise_hint` on a post-cascade 402.

Contract: `change-pipeline/quota-raise-and-archive/CONTRACT-F3-surfaces.md`, id R36.

  "The 402 a user sees is the end of a priced cascade: the model she wanted
  needed $12 and the cheapest fallback $0.40, so a hint without those numbers
  asks her to invent one. Unit: the hint carries per-candidate `model_id`,
  `estimated_cost_microusd`, `shortfall_microusd`, `blocker`, `grantable`, plus
  `minimum_raise_microusd`, the target candidate's shortfall, `router_mode`,
  the pricing version and `priced_at`; four internal reserve refusals produce
  one hint."

This exercises the REAL, already-merged cascade
(`mvp._pipeline._reserve_over_candidates`, `backend/mvp/_pipeline.py:2373`),
the same machinery `test_quota_cascade.py::TestCascade::test_all_quotas_exhausted_raises_402`
already drives to a bare `{"reason": "model_quota_exhausted"}`. F3's job is to
attach `detail["raise_hint"]` at that exact refusal site. Today it does not —
these tests fail on that absence, not on any seeding gap: everything used to
build the fixture (routing config, per-model quotas, the pool) already exists
and is exercised by test_quota_cascade.py.

Contract correction — `blocker` and `grantable` are pinned exactly, not left
as this role's reading: `blocker` is a closed four-value enum
(`tenant_pool | user_token_quota | per_model_tenant | per_model_user`) naming
WHICH WALL stopped the candidate, and `grantable` is `true` for `tenant_pool`
alone — a self-service limit raise only ever raises the tenant pool ceiling,
never a per-model or personal-token quota.

This has a real structural consequence, verified against the ACTUAL code
rather than assumed: `reserve_credit()`'s pool-exhaustion precheck
(`_pipeline.py:2981-2992`) raises `_err_402("tenant_pool_exhausted")`
directly — NOT `QuotaExhausted` — so `_reserve_over_candidates`'s
`except QuotaExhausted: continue` never catches it; a pool-exhausted
candidate ends the WHOLE cascade on whichever candidate hits it first, never
letting a cheaper candidate get a real try. Only per-model quota exhaustion
(`QuotaExhausted`, raised via the transaction's `ConditionalCheckFailed` at
`_pipeline.py:3121`) is catchable and lets the cascade advance. Consequently:

  - `TestRaiseHintPresence` below drives a 4-deep PER-MODEL-QUOTA cascade
    (the only way to structurally exercise "four internal reserve refusals
    produce one hint" against the real code) — every candidate's blocker is
    `per_model_tenant`, and NONE is grantable, so `minimum_raise_microusd`
    must be `0` (nothing a personal raise can do).
  - `TestTenantPoolBlockerIsGrantable` below drives a SEPARATE, single-pin
    scenario that hits the pool wall directly, to pin `blocker == "tenant_pool"`
    and `grantable is True` concretely, without depending on the cascade
    ever trying more than one candidate against the pool (it structurally
    cannot, per the above).

Seam amendments (B2/B3/B5/B6, the integration owner's seam notes — §S7/§S11/§S12,
outside this repository) land in this file:

  - **B5** promotes the structural finding above from this role's own reading
    into the contract, and RETRACTS this file's earlier design: the hint may
    NOT classify an untried tail via "read-only recomputation" of pool/quota
    headroom (what `TestTenantPoolBlockerIsGrantable`'s docstring used to
    say). Gathering that data means pricing it, and pricing on the refusal
    path was deliberately removed. `TestPoolRefusalMidCascadeNamesOnlyWhatWasPriced`
    below drives a REAL multi-candidate chain (not a pin) where the pool wall
    hits the FIRST candidate, to pin that `candidates` has exactly one entry
    and the rest are named — by id only, no cost data — in a new
    `unattempted_model_ids` field.
  - **B2/B3** move the `RaiseHint`/`RaiseHintCandidate` schema to F2
    (`mvp.grants`, this role's placement guess), shipped with degenerate
    one-candidate content; F3 fills it with zero renames. `TestHintConformsToF2Model`
    is the new conformance test B2 requires; `TestF2EraClientAgainstF3Backend`
    is the new skew-testing leg B3 requires (an F2-era client parsing F3's
    richer hint).
  - **B6** adds `remaining_cap_microusd` (F2-owned) to the envelope, asserted
    in `test_raise_hint_top_level_fields` below; the conflict-rendering half
    (do not pre-fill an unfulfillable amount) is a frontend concern, tested in
    `frontend/src/pages/MeLimitRaises.test.tsx`, not here.
"""
from __future__ import annotations

from dataclasses import dataclass

import boto3
import pytest
from fastapi import HTTPException

from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
from dynamo.user_tenants import UserTenantsRepository
from mvp import _pipeline
from mvp.routing.config import _cache as _cfg_cache


@dataclass
class _User:
    user_id: str
    org_id: str
    email: str = "u@example.com"


TENANT = "raise-hint-org"
USER = "user-raise-hint-0001"

# Four candidates so the cascade must internally raise QuotaExhausted four
# times before the caller ever sees the one 402 the id names ("four internal
# reserve refusals produce one hint").
CHAIN = [
    "claude-opus-4-7",   # the target — what she actually asked for
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",  # the cheapest fallback
]


@pytest.fixture
def env(dynamodb_mock):
    _cfg_cache.clear()
    UserTenantsRepository().ensure(
        user_id=USER, tenant_id=TENANT, role="user", total_credit=10**12,
    )
    TenantBudgetsRepository().set_manual_limit(
        tenant_id=TENANT, period=current_period(), manual_limit_microusd=10**11,
    )
    yield
    _cfg_cache.clear()


def _put_routing_config(**item):
    tbl = boto3.resource("dynamodb", region_name="us-east-1").Table(
        "stratoclave-user-tenants")
    tbl.put_item(Item={"user_id": "CONFIG#ROUTING", "tenant_id": TENANT, **item})
    _cfg_cache.clear()


def _reserve_all_exhausted():
    """Every candidate in CHAIN has a ~zero quota: the cascade tries all four,
    exhausts all four, and the caller sees exactly one HTTPException."""
    _put_routing_config(
        chain=CHAIN,
        quotas={m: {"limit": 1} for m in CHAIN},
        fallback_default="on",
    )
    return _pipeline.reserve_credit_for_model(
        _User(user_id=USER, org_id=TENANT),
        1000,
        model_name=CHAIN[0],
        input_tokens_est=500,
        max_output_tokens=500,
        wire_protocol="messages",
    )


class TestRaiseHintPresence:
    def test_402_carries_a_raise_hint_object(self, env):
        with pytest.raises(HTTPException) as e:
            _reserve_all_exhausted()
        assert e.value.status_code == 402
        assert e.value.detail["reason"] == "model_quota_exhausted"
        # This is the whole defect R36 exists to close: today `_err_402`
        # returns only {"type", "reason", "message"} — no `raise_hint` key at
        # all, on ANY 402, cascade or not.
        assert "raise_hint" in e.value.detail, (
            "402 detail has no raise_hint — a user sees `model_quota_exhausted` "
            "with no candidate, no cost, no shortfall: exactly the "
            "'asks her to invent one' defect R36 names."
        )

    def test_raise_hint_names_every_tried_candidate(self, env):
        with pytest.raises(HTTPException) as e:
            _reserve_all_exhausted()
        hint = e.value.detail["raise_hint"]
        candidate_ids = {c["model_id"] for c in hint["candidates"]}
        # All four internal refusals must be represented, not just the last.
        assert candidate_ids == set(CHAIN), (
            f"raise_hint.candidates named {candidate_ids!r}, expected all "
            f"four cascade candidates {set(CHAIN)!r} — the id's own "
            f"Verified-by is 'four internal reserve refusals produce one hint'."
        )

    #: The four canonical `blocker` values (contract-pinned). Any other
    #: string, including this role's earlier reading
    #: (`model_quota_exhausted`, `personal_budget_exhausted`, ...), is wrong.
    VALID_BLOCKERS = frozenset(
        {"tenant_pool", "user_token_quota", "per_model_tenant", "per_model_user"}
    )

    def test_raise_hint_per_candidate_fields(self, env):
        with pytest.raises(HTTPException) as e:
            _reserve_all_exhausted()
        hint = e.value.detail["raise_hint"]
        for c in hint["candidates"]:
            for field in (
                "model_id", "estimated_cost_microusd", "shortfall_microusd",
                "blocker", "grantable",
            ):
                assert field in c, f"candidate {c} missing required field {field!r}"
            assert isinstance(c["estimated_cost_microusd"], int)
            # `shortfall_microusd` is a MEASURED figure, not a derived one
            # (B5's own principle, applied one wall over from the case it
            # names): a per-model quota's ConditionalCheckFailed tells the
            # gateway only THAT the ADD was refused, never the counter's
            # remaining headroom (verified against real code --
            # `QuotaExhausted` at `_pipeline.py:2124` carries only `model`
            # and `scope`, no headroom fact -- and no
            # `ReturnValuesOnConditionCheckFailure` is requested on that
            # transaction). Every candidate in THIS scenario is blocked by
            # `per_model_tenant`, so `shortfall_microusd` is `None` here by
            # construction; asserting an `int` unconditionally would demand
            # a measurement the refusal path never took, which is exactly
            # the defect class B5 exists to prevent. The tenant-pool case
            # (where the refusal DOES already hold the row, so the shortfall
            # IS measured) is pinned separately in
            # `TestTenantPoolBlockerIsGrantable`.
            if c["blocker"] == "tenant_pool":
                assert isinstance(c["shortfall_microusd"], int)
                assert c["shortfall_microusd"] >= 0
            else:
                assert c["shortfall_microusd"] is None or (
                    isinstance(c["shortfall_microusd"], int)
                    and c["shortfall_microusd"] >= 0
                )
            assert isinstance(c["grantable"], bool)
            assert c["blocker"] in self.VALID_BLOCKERS, (
                f"blocker {c['blocker']!r} is not one of the four contract-pinned "
                f"values {sorted(self.VALID_BLOCKERS)!r}"
            )
            # Pinned rule: grantable is true for tenant_pool ALONE.
            assert c["grantable"] == (c["blocker"] == "tenant_pool"), (
                f"candidate {c} — grantable must track (blocker == 'tenant_pool') "
                "exactly, per the contract correction"
            )

    def test_all_four_cascade_candidates_are_blocked_by_the_tenant_scoped_model_quota(
        self, env
    ):
        # This scenario configures a TENANT-scoped per-model limit
        # (`quotas={m: {"limit": 1}}`, no per-user scope) — the same wall
        # `test_quota_cascade.py` exercises as `model_quota_exhausted`. Under
        # the corrected enum that wall is named `per_model_tenant`, and it is
        # NOT grantable — only `tenant_pool` is.
        with pytest.raises(HTTPException) as e:
            _reserve_all_exhausted()
        hint = e.value.detail["raise_hint"]
        for c in hint["candidates"]:
            assert c["blocker"] == "per_model_tenant", c
            assert c["grantable"] is False, c

    def test_raise_hint_top_level_fields(self, env):
        with pytest.raises(HTTPException) as e:
            _reserve_all_exhausted()
        hint = e.value.detail["raise_hint"]
        for field in (
            "minimum_raise_microusd", "target_shortfall_microusd",
            "router_mode", "pricing_version", "priced_at", "tenant_id",
            "remaining_cap_microusd",  # B6: F2-owned, required so the
            # self-service view can detect an unfulfillable minimum_raise
            # BEFORE pre-filling it.
            "unattempted_model_ids",  # B5: names-only tail, present
            # (possibly empty) on every hint, never inferred from cost data
            # the refusal path never gathered.
        ):
            assert field in hint, f"raise_hint missing required top-level field {field!r}"
        assert hint["tenant_id"] == TENANT
        # The target is what she actually asked for, first in the chain.
        assert hint.get("requested_model_id") == CHAIN[0]
        assert isinstance(hint["remaining_cap_microusd"], int)
        assert hint["remaining_cap_microusd"] >= 0

    def test_unattempted_model_ids_is_empty_when_every_configured_candidate_was_priced(
        self, env
    ):
        # B5's OTHER half: a quota cascade is NOT a pool refusal — every
        # configured candidate genuinely gets tried (QuotaExhausted is
        # catchable), so there is no untried tail to name here. Contrast
        # with TestPoolRefusalMidCascadeNamesOnlyWhatWasPriced below, where
        # the same four-model chain DOES leave a real, non-empty tail.
        with pytest.raises(HTTPException) as e:
            _reserve_all_exhausted()
        hint = e.value.detail["raise_hint"]
        assert hint["unattempted_model_ids"] == []

    def test_minimum_raise_is_zero_when_no_candidate_is_grantable(self, env):
        # Every candidate in THIS scenario is blocked by a per-model tenant
        # quota (`per_model_tenant`), and only `tenant_pool` is grantable —
        # so a personal limit raise can fix NONE of them.
        # minimum_raise_microusd must reflect that (0 / nothing to ask for),
        # never silently fall back to the target's own (non-grantable)
        # shortfall — that would send her to ask for a raise that would not
        # actually clear anything.
        with pytest.raises(HTTPException) as e:
            _reserve_all_exhausted()
        hint = e.value.detail["raise_hint"]
        assert not any(c["grantable"] for c in hint["candidates"])
        assert hint["minimum_raise_microusd"] == 0

    def test_one_refusal_produces_exactly_one_hint(self, env):
        # "one refusal produces one raise event" — the internal QuotaExhausted
        # advances must never each independently raise a 402/hint; only the
        # single HTTPException that reaches the caller carries one.
        with pytest.raises(HTTPException) as e:
            _reserve_all_exhausted()
        # There is exactly one HTTPException instance raised for the whole
        # cascade (pytest.raises already proves this structurally — this
        # assertion pins that its `raise_hint` is a single object, not a list
        # of per-advance hints).
        assert isinstance(e.value.detail.get("raise_hint"), dict)


POOL_TENANT = "raise-hint-pool-org"
POOL_USER = "user-raise-hint-pool-0001"
POOL_MODEL = "claude-opus-4-7"


def _reserve_pinned_pool_probe():
    return _pipeline.reserve_credit_for_model(
        _User(user_id=POOL_USER, org_id=POOL_TENANT),
        1000,
        model_name=POOL_MODEL,
        input_tokens_est=500,
        max_output_tokens=500,
        wire_protocol="messages",
        vsr_hard_model=POOL_MODEL,  # a pin: router_mode == "pin"
    )


@pytest.fixture
def pool_env(dynamodb_mock):
    """A tenant whose pool starts GENEROUS, gets consumed down to a sliver by
    one real, successful reservation, and is then re-sized so the identically
    priced SECOND attempt genuinely exhausts it — `cost <= pool_limit` (so it
    is ordinary exhaustion, not the separate "cannot fit at all" refusal) but
    `reserved + cost > pool_limit`. No per-model quota is configured at all,
    so `tenant_pool` is the ONLY wall in play."""
    _cfg_cache.clear()
    UserTenantsRepository().ensure(
        user_id=POOL_USER, tenant_id=POOL_TENANT, role="user", total_credit=10**12,
    )
    period = current_period()
    TenantBudgetsRepository().set_manual_limit(
        tenant_id=POOL_TENANT, period=period, manual_limit_microusd=10**11,
    )
    # Probe reservation: generous pool, so this succeeds and reveals the
    # real priced cost for POOL_MODEL under this request shape.
    probe_ctx = _reserve_pinned_pool_probe()
    cost = probe_ctx.pool_reserved_microusd
    assert cost > 0, "probe reservation priced at 0 — cannot build a pool-exhaustion case"
    # Re-size the pool to 1.5x one reservation's cost: the probe's own
    # `reserved` (already on the pool row) plus a second, IDENTICALLY priced
    # attempt exceeds it, while the second attempt's cost alone does not.
    TenantBudgetsRepository().set_manual_limit(
        tenant_id=POOL_TENANT, period=period, manual_limit_microusd=int(cost * 1.5),
    )
    yield
    _cfg_cache.clear()


class TestTenantPoolBlockerIsGrantable:
    """`blocker == "tenant_pool"` is the ONE value `grantable` is true for.

    Verified against real code: `reserve_credit()`'s pool precheck
    (`_pipeline.py:2981-2992`) raises `_err_402("tenant_pool_exhausted")`
    directly, uncaught by the cascade's `except QuotaExhausted` — so this is
    necessarily a SINGLE-candidate (pinned) scenario here: a pin's chain has
    exactly one configured candidate, so there is no tail to name either way.
    `TestPoolRefusalMidCascadeNamesOnlyWhatWasPriced` below drives the case
    that DOES have a tail — a real chain, where the pool wall hits the first
    candidate and the rest are named but never priced (B5).
    """

    def test_pool_exhausted_refusal_names_tenant_pool_as_grantable(self, pool_env):
        with pytest.raises(HTTPException) as e:
            _pipeline.reserve_credit_for_model(
                _User(user_id=POOL_USER, org_id=POOL_TENANT),
                1000,
                model_name=POOL_MODEL,
                input_tokens_est=500,
                max_output_tokens=500,
                wire_protocol="messages",
                vsr_hard_model=POOL_MODEL,  # a pin: router_mode == "pin"
            )
        assert e.value.status_code == 402
        assert e.value.detail["reason"] == "tenant_pool_exhausted"
        hint = e.value.detail["raise_hint"]
        target = next(c for c in hint["candidates"] if c["model_id"] == POOL_MODEL)
        assert target["blocker"] == "tenant_pool"
        assert target["grantable"] is True
        assert target["shortfall_microusd"] > 0
        # The only grantable candidate IS the target here — minimum_raise
        # must equal its shortfall exactly, not 0 and not some other number.
        assert hint["minimum_raise_microusd"] == target["shortfall_microusd"]
        assert hint["router_mode"] == "pin"
        # A pin has exactly one configured candidate — nothing was left
        # unattempted, so the field must be present but empty, never absent.
        assert hint["unattempted_model_ids"] == []


POOL_CASCADE_TENANT = "raise-hint-pool-cascade-org"
POOL_CASCADE_USER = "user-raise-hint-pool-cascade-0001"


def _reserve_cascade_probe():
    """No pin — a genuine cascade attempt on CHAIN[0], no per-model quota
    configured at all, so nothing but the pool can block it."""
    return _pipeline.reserve_credit_for_model(
        _User(user_id=POOL_CASCADE_USER, org_id=POOL_CASCADE_TENANT),
        1000,
        model_name=CHAIN[0],
        input_tokens_est=500,
        max_output_tokens=500,
        wire_protocol="messages",
    )


@pytest.fixture
def pool_cascade_env(dynamodb_mock):
    """A REAL fallback chain (all four CHAIN models, no per-model quotas at
    all) whose pool is sized so the FIRST candidate alone exhausts it —
    demonstrating B5's point with more than one candidate actually
    configured: the pool wall still ends the cascade on candidate 0, so
    candidates 1-3 are genuinely never priced, not merely unlucky to be
    priced last."""
    _cfg_cache.clear()
    UserTenantsRepository().ensure(
        user_id=POOL_CASCADE_USER, tenant_id=POOL_CASCADE_TENANT, role="user",
        total_credit=10**12,
    )
    period = current_period()
    TenantBudgetsRepository().set_manual_limit(
        tenant_id=POOL_CASCADE_TENANT, period=period, manual_limit_microusd=10**11,
    )
    tbl = boto3.resource("dynamodb", region_name="us-east-1").Table(
        "stratoclave-user-tenants")
    tbl.put_item(Item={
        "user_id": "CONFIG#ROUTING", "tenant_id": POOL_CASCADE_TENANT,
        "chain": CHAIN, "quotas": {}, "fallback_default": "on",
    })
    _cfg_cache.clear()
    probe_ctx = _reserve_cascade_probe()
    cost = probe_ctx.pool_reserved_microusd
    assert cost > 0, "probe reservation priced at 0 — cannot build a pool-exhaustion case"
    TenantBudgetsRepository().set_manual_limit(
        tenant_id=POOL_CASCADE_TENANT, period=period, manual_limit_microusd=int(cost * 1.5),
    )
    yield
    _cfg_cache.clear()


class TestPoolRefusalMidCascadeNamesOnlyWhatWasPriced:
    """B5, promoted from this role's own finding: at a pool refusal, exactly
    ONE candidate was priced — even when a real, multi-candidate chain was
    configured. The hint must say the rest were not attempted, and must NOT
    claim cost/shortfall/blocker data for them (the earlier "read-only
    recomputation" design this file held before B5 is wrong: gathering that
    data means pricing it, and pricing on the refusal path was removed).
    """

    def test_exactly_one_candidate_is_priced_and_the_rest_are_named_not_costed(
        self, pool_cascade_env
    ):
        with pytest.raises(HTTPException) as e:
            _reserve_cascade_probe()  # second call: pool is now too small
        assert e.value.status_code == 402
        assert e.value.detail["reason"] == "tenant_pool_exhausted"
        hint = e.value.detail["raise_hint"]

        assert len(hint["candidates"]) == 1, (
            f"expected exactly one PRICED candidate at a pool refusal, got "
            f"{len(hint['candidates'])}: {hint['candidates']!r} — a pool "
            f"refusal leaves the cascade loop immediately (_pipeline.py:2457), "
            f"so candidates 1-3 of CHAIN were never priced"
        )
        priced = hint["candidates"][0]
        assert priced["model_id"] == CHAIN[0]
        assert priced["blocker"] == "tenant_pool"
        assert priced["grantable"] is True

        # The rest of the configured chain, named but carrying NO cost data.
        assert hint["unattempted_model_ids"] == CHAIN[1:], (
            f"expected the untried tail named by id only: {CHAIN[1:]!r}, got "
            f"{hint['unattempted_model_ids']!r}"
        )
        assert hint["router_mode"] == "cascade"  # contrast with the pin test above

    def test_unattempted_ids_carry_no_cost_or_blocker_fields(self, pool_cascade_env):
        # A structural check that `unattempted_model_ids` is a list of plain
        # strings — never dicts smuggling in cost/blocker data the refusal
        # path never gathered (which is what this file's pre-B5 design would
        # have produced).
        with pytest.raises(HTTPException) as e:
            _reserve_cascade_probe()
        hint = e.value.detail["raise_hint"]
        for entry in hint["unattempted_model_ids"]:
            assert isinstance(entry, str), (
                f"unattempted_model_ids must be plain model-id strings, got "
                f"{entry!r} — a dict here would imply cost data that was "
                f"never gathered"
            )


class TestHintConformsToF2Model:
    """B2 — the conformance test the amendment requires: every hint F3 fills
    must validate against F2's SHIPPED model, not this file's own prose
    description of the shape. `mvp.grants` does not exist in this worktree
    (F2 has not landed here), so this fails at import — the same "surface
    absent" reason as every other F1/F2-owned surface in this suite, applied
    here to a schema rather than an endpoint.
    """

    def test_a_quota_cascade_hint_validates_against_f2s_raise_hint_model(self, env):
        from mvp.grants import RaiseHint  # F2-owned; does not exist yet

        with pytest.raises(HTTPException) as e:
            _reserve_all_exhausted()
        hint = e.value.detail["raise_hint"]
        # Pydantic v2 style; validates the exact dict F3 built against F2's
        # shipped model with zero renames/removals (B2's own requirement).
        RaiseHint.model_validate(hint)

    def test_a_pool_refusal_hint_with_a_short_candidate_list_also_conforms(
        self, pool_cascade_env
    ):
        # The degenerate case F2 ships BY DEFAULT (one candidate) must
        # validate too — F3's richer, multi-candidate hints are a
        # superset, never a divergent shape.
        from mvp.grants import RaiseHint

        with pytest.raises(HTTPException) as e:
            _reserve_cascade_probe()
        hint = e.value.detail["raise_hint"]
        assert len(hint["candidates"]) == 1
        RaiseHint.model_validate(hint)


class TestF2EraClientAgainstF3Backend:
    """B3 — the skew-testing leg this role's original verification plan was
    missing: not only "an old console against a new API," but an F2-era
    CLIENT (F2's CLI, the one that actually exists in the interval) reading
    a hint an F3 backend produced. F2's own model is degenerate (one
    candidate); this asserts that model can still parse F3's richer hint
    without raising and without losing the ONE candidate an F2-era client
    knows how to render — extra candidates and the new `unattempted_model_ids`
    field are simply additional data an old client ignores, never a parse
    failure. Fails at the same import as the conformance test above; this is
    a distinct test because "validates" (Pydantic accepts extra fields under
    F2's model config) is a different claim from "an old client's OWN code
    path, reading only candidates[0], still gets sensible data."
    """

    def test_f2_eras_single_candidate_read_still_gets_the_priced_candidate(self, env):
        from mvp.grants import RaiseHint  # F2-owned; does not exist yet

        with pytest.raises(HTTPException) as e:
            _reserve_all_exhausted()
        hint = e.value.detail["raise_hint"]
        # An F2-era client parses with F2's own model, then reads exactly
        # what F2's contract promised it: candidates[0].
        f2_view = RaiseHint.model_validate(hint)
        assert f2_view.candidates[0].model_id == CHAIN[0]
        assert f2_view.candidates[0].blocker is not None
