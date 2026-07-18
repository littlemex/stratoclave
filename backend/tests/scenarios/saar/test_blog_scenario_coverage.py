"""Exhaustive coverage of the vLLM SAAR blog's nine scenarios against
Stratoclave's SAAR decision core.

Every blog claim in blog_scenarios.SCENARIOS has a test here. A claim marked
NOT_IMPLEMENTED gets an xfail(strict=True) test that PINS the gap: it asserts
Stratoclave does NOT (yet) enforce it, so the day someone wires it up the xfail
flips to XPASS and this suite fails loudly — the catalogue can never drift out of
sync with reality. A claim marked COVERED_ELSEWHERE gets a presence assertion +
a pointer to the owning unit/formal test (kept DRY, not re-implemented here).

Run just this suite:  pytest -m saar_scenario
"""
from __future__ import annotations

import pytest

from mvp.routing import saar

from . import blog_scenarios as bs

pytestmark = pytest.mark.saar_scenario


# ---------------------------------------------------------------------------
# meta: the catalogue itself is complete and self-consistent
# ---------------------------------------------------------------------------

def test_all_nine_blog_scenarios_present():
    # The blog makes exactly nine claims; the catalogue must carry all nine with
    # a valid coverage verdict (no silent omission, no bogus status).
    assert len(bs.SCENARIOS) == 9
    valid = {bs.COVERED, bs.COVERED_ELSEWHERE, bs.NOT_IMPLEMENTED}
    for s in bs.SCENARIOS:
        assert s.coverage in valid, s.id
        if s.coverage == bs.COVERED_ELSEWHERE:
            assert s.owner_test, f"{s.id} must name its owning test"


# ---------------------------------------------------------------------------
# 1. tool-loop hard lock — a tool return MUST go back to the loop's model
# ---------------------------------------------------------------------------

def test_tool_loop_hard_lock_returns_to_same_model():
    # turn 0: normal request that emits a tool_use (opens the loop on 'opus').
    # turn 1: returns the tool result — even though the cascade would pick 'haiku',
    #         SAAR must hard-lock back to 'opus'.
    turns = [
        bs.Turn(emits_tool_use=True, cascade_choice="opus", at_epoch=1000),
        bs.Turn(has_tool_result=True, cascade_choice="haiku", at_epoch=1001),
    ]
    out = bs.drive_session(turns)
    assert out.results[1].hard_locked is True
    assert out.results[1].committed_model == "opus"   # NOT the cascade's haiku
    assert out.results[1].reason == "tool-loop-lock"
    assert out.violations == 0


def test_tool_loop_lock_only_fires_with_a_tool_result():
    # A session in tool-loop phase whose next turn is a PLAIN question (no tool
    # result) must NOT stay hard-locked — it soft-sticks and can reselect.
    turns = [
        bs.Turn(emits_tool_use=True, cascade_choice="opus", at_epoch=1000),
        bs.Turn(has_tool_result=False, cascade_choice="haiku", at_epoch=1001),
    ]
    out = bs.drive_session(turns)
    assert out.results[1].hard_locked is False
    # soft-prefers the warm model (sticky), so it stays on opus but was free to move
    assert out.results[1].reason == "sticky"


# ---------------------------------------------------------------------------
# 2. idle-timeout reset
# ---------------------------------------------------------------------------

def test_idle_timeout_reset_reopens_selection():
    turns = [
        bs.Turn(cascade_choice="opus", at_epoch=1000),
        # 301s later (> 300s boundary): continuity decays, cascade may reselect.
        bs.Turn(cascade_choice="haiku", at_epoch=1000 + 301),
    ]
    out = bs.drive_session(turns, idle_reset_seconds=300)
    assert out.results[1].reason == "reset"
    assert out.results[1].committed_model == "haiku"  # cascade won, no sticky pin


def test_within_idle_boundary_stays_sticky():
    turns = [
        bs.Turn(cascade_choice="opus", at_epoch=1000),
        bs.Turn(cascade_choice="haiku", at_epoch=1000 + 299),  # < 300s
    ]
    out = bs.drive_session(turns, idle_reset_seconds=300)
    assert out.results[1].reason == "sticky"
    assert out.results[1].committed_model == "opus"  # soft-prefer held


# ---------------------------------------------------------------------------
# 3. decision-drift reset
# ---------------------------------------------------------------------------

def test_decision_drift_reopens_selection():
    turns = [
        bs.Turn(cascade_choice="opus", matched_decision="code-edit", at_epoch=1000),
        # task shape changed => drift => reselect
        bs.Turn(cascade_choice="haiku", matched_decision="synthesis", at_epoch=1001),
    ]
    out = bs.drive_session(turns)
    assert out.results[1].reason == "drift"
    assert out.results[1].committed_model == "haiku"


def test_same_decision_stays_sticky():
    turns = [
        bs.Turn(cascade_choice="opus", matched_decision="code-edit", at_epoch=1000),
        bs.Turn(cascade_choice="haiku", matched_decision="code-edit", at_epoch=1001),
    ]
    out = bs.drive_session(turns)
    assert out.results[1].reason == "sticky"
    assert out.results[1].committed_model == "opus"


# ---------------------------------------------------------------------------
# 4. "SAAR is not just sticky" — reduces switches YET reselects on task change
# ---------------------------------------------------------------------------

def test_saar_reduces_switches_but_still_moves_on_drift():
    # 6 turns, same task for the first 3 (cascade would flap opus/haiku/opus),
    # then the task changes for the last 3.
    turns = [
        bs.Turn(cascade_choice="opus", matched_decision="code", at_epoch=1000),
        bs.Turn(cascade_choice="haiku", matched_decision="code", at_epoch=1001),
        bs.Turn(cascade_choice="opus", matched_decision="code", at_epoch=1002),
        bs.Turn(cascade_choice="sonnet", matched_decision="synth", at_epoch=1003),
        bs.Turn(cascade_choice="sonnet", matched_decision="synth", at_epoch=1004),
        bs.Turn(cascade_choice="haiku", matched_decision="synth", at_epoch=1005),
    ]
    saar_out = bs.drive_session(turns)
    no_saar = bs.drive_no_saar(turns)
    # SAAR strictly reduces switches vs the no-SAAR cascade...
    assert saar_out.switches < no_saar.switches
    # ...but it is NOT a hard sticky pin: the drift at turn 3 DID move the model
    # (a pure hard-sticky router would have frozen on turn 0's model forever).
    assert saar_out.results[3].reason == "drift"
    assert saar_out.results[3].committed_model != saar_out.results[2].committed_model


# ---------------------------------------------------------------------------
# 5 & 6. covered elsewhere — assert the surface exists + point to the owner
# ---------------------------------------------------------------------------

def test_prefix_cache_checkout_pricing_surface_exists():
    s = bs.by_id("prefix-cache-checkout-pricing")
    assert s.coverage == bs.COVERED_ELSEWHERE and s.owner_test
    # the priced-switch surface the blog describes IS the checkout delta; assert
    # the module exposes it (value semantics are property-tested in owner_test).
    from mvp import pricing
    assert callable(pricing.saar_checkout_delta_microusd)


def test_router_memory_scope_is_routing_only():
    s = bs.by_id("router-memory-scope")
    assert s.coverage == bs.COVERED_ELSEWHERE and s.owner_test
    # SessionMemory must carry ONLY routing fields — never message/conversation
    # content (the blog's explicit scope boundary).
    fields = set(saar.SessionMemory.__dataclass_fields__)
    assert {"last_physical_model", "phase", "matched_decision", "switch_count",
            "turn_count", "last_turn_at", "warm_prefix_tokens"} <= fields
    forbidden = {"messages", "conversation", "history", "prompt", "content",
                 "user_profile", "retrieval"}
    assert not (fields & forbidden)


# ---------------------------------------------------------------------------
# 7. fault recovery — a memory read failure fails OPEN, invariants survive
# ---------------------------------------------------------------------------

def test_fault_recovery_read_failure_fails_open(monkeypatch):
    # Simulate the 503/timeout the blog injects: load_session_memory returns None.
    # The decision core must then behave as a cold session (no crash), and — the
    # invariant the blog stresses — the tool-loop lock is RE-DERIVED from this
    # request's own content, so a lost read cannot route a tool return to the
    # wrong model on the SAME turn.
    d = saar.decide(mem=None, now_epoch=1000, request_has_tool_result=True,
                    matched_decision=None)
    assert d.reason == "cold" and d.hard_model is None  # no crash, no opinion

    # And end-to-end through saar_pre_reserve with a forced read failure.
    monkeypatch.setenv("SAAR_ENABLED", "true")
    monkeypatch.setattr(saar, "load_session_memory",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("503")))

    class _Ctx:
        tenant_id = "acme"
        workflow_run_id = "wf"
        def session_key(self):
            return "sess-1"

    # must not raise; fail-open returns a context OR None, never an exception.
    try:
        res = saar.saar_pre_reserve(ctx=_Ctx(), org_id="acme", user_id="u",
                                    request_messages=[], matched_decision=None)
    except Exception as e:  # noqa: BLE001
        pytest.fail(f"saar_pre_reserve must fail open, raised {e!r}")
    # a raised read is caught inside pre_reserve -> None (silent cascade).
    assert res is None


def test_fault_recovery_repeated_failures_zero_violations():
    # 168 repeated failures across a tool-loop session: because the lock is
    # re-derived from the request content (not solely from possibly-lost memory),
    # a tool return that arrives with NO memory just cold-starts — it never
    # commits to a DIFFERENT model claiming the loop is intact. Zero violations.
    outs = []
    for _ in range(168):
        d = saar.decide(mem=None, now_epoch=0, request_has_tool_result=True,
                        matched_decision=None)
        outs.append(d)
    assert all(o.reason == "cold" and o.hard_model is None for o in outs)


# ---------------------------------------------------------------------------
# 8. live zero-violations — a multi-session deterministic workload
# ---------------------------------------------------------------------------

def _tool_loop_workload(n_sessions: int, turns_per: int) -> int:
    """Drive N independent tool-loop sessions; return total violations. Each
    session alternates: emit tool_use, then return the tool result while the
    cascade tries to yank the model elsewhere. A correct lock => 0 violations."""
    total_violations = 0
    for s in range(n_sessions):
        turns: list[bs.Turn] = []
        base = 1000 + s * 10_000
        for k in range(turns_per):
            if k % 2 == 0:
                turns.append(bs.Turn(emits_tool_use=True, cascade_choice="opus",
                                     at_epoch=base + k))
            else:
                # the cascade WANTS to move to a cheaper model mid-loop
                turns.append(bs.Turn(has_tool_result=True, cascade_choice="haiku",
                                     at_epoch=base + k))
        total_violations += bs.drive_session(turns).violations
    return total_violations


def test_live_workload_zero_continuity_violations():
    # 60 sessions x 20 turns = 1,200 turns of adversarial mid-loop cascade pull.
    assert _tool_loop_workload(n_sessions=60, turns_per=20) == 0


# ---------------------------------------------------------------------------
# 9. provider-state lock — a continuation reference hard-locks to its backend
# ---------------------------------------------------------------------------

def test_provider_state_lock_is_now_covered():
    assert bs.by_id("provider-state-lock").coverage == bs.COVERED


def test_provider_state_lock_returns_to_origin_backend():
    # turn 0: a normal request whose response mints id 'resp_A' (opens the
    #         provider-state phase on 'opus').
    # turn 1: references EXACTLY 'resp_A' — even though the cascade would pick
    #         'haiku', SAAR must hard-lock back to the minting backend 'opus'.
    turns = [
        bs.Turn(emitted_response_id="resp_A", cascade_choice="opus", at_epoch=1000),
        bs.Turn(references_response_id="resp_A", cascade_choice="haiku", at_epoch=1001),
    ]
    out = bs.drive_session(turns)
    assert out.results[1].hard_locked is True
    assert out.results[1].committed_model == "opus"     # NOT the cascade's haiku
    assert out.results[1].reason == "provider-state-lock"
    assert out.violations == 0


def test_provider_state_lock_rejects_forged_or_foreign_id():
    # A client that sends an id the session never minted must NOT lock: the
    # verified-id design defeats forced/wrong-backend locking (Fable review §3).
    turns = [
        bs.Turn(emitted_response_id="resp_A", cascade_choice="opus", at_epoch=1000),
        bs.Turn(references_response_id="resp_FORGED", cascade_choice="haiku", at_epoch=1001),
    ]
    out = bs.drive_session(turns)
    assert out.results[1].hard_locked is False
    # unverified id => no lock => the turn is free to move (soft sticky, here).
    assert out.results[1].reason != "provider-state-lock"


def test_provider_state_lock_survives_a_long_idle_gap_within_cap():
    # THE distinguishing invariant vs the tool-loop lock: a verified continuation
    # id is bound to its backend across an idle gap that WOULD reset a tool loop,
    # PROVIDED the gap is within the provider-state hard cap.
    turns = [
        bs.Turn(emitted_response_id="resp_A", cascade_choice="opus", at_epoch=1000),
        # 1800s later (> 300s idle boundary, < 3600s hard cap): still references A.
        bs.Turn(references_response_id="resp_A", cascade_choice="haiku",
                at_epoch=1000 + 1800),
    ]
    out = bs.drive_session(turns, idle_reset_seconds=300)
    assert out.results[1].reason == "provider-state-lock"
    assert out.results[1].committed_model == "opus"


def test_provider_state_lock_yields_past_hard_cap():
    # Escape hatch: past the hard cap, even a still-referenced continuation is
    # freed (a retired/dead backend can't strand the session forever, Fable §1).
    turns = [
        bs.Turn(emitted_response_id="resp_A", cascade_choice="opus", at_epoch=1000),
        # 100_000s later (>> 3600s hard cap): the lock yields to idle reset.
        bs.Turn(references_response_id="resp_A", cascade_choice="haiku",
                at_epoch=1000 + 100_000),
    ]
    out = bs.drive_session(turns, idle_reset_seconds=300)
    assert out.results[1].reason == "reset"
    assert out.results[1].committed_model == "haiku"  # freed to reselect


def test_provider_state_zero_violations_under_workload():
    # 40 sessions, each: mint a continuation, then reference EXACTLY it while the
    # cascade tries to yank the backend. A correct lock => 0 violations.
    total = 0
    for s in range(40):
        base = 5000 + s * 1000
        rid1, rid2 = f"resp_{s}_1", f"resp_{s}_2"
        turns = [
            bs.Turn(emitted_response_id=rid1, cascade_choice="opus", at_epoch=base),
            bs.Turn(references_response_id=rid1, emitted_response_id=rid2,
                    cascade_choice="haiku", at_epoch=base + 1),
            bs.Turn(references_response_id=rid2, cascade_choice="sonnet",
                    at_epoch=base + 2),
        ]
        total += bs.drive_session(turns).violations
    assert total == 0


# ---------------------------------------------------------------------------
# metrics reproduction — the headline figures, deterministic & regression-gated
# ---------------------------------------------------------------------------

def test_metrics_reproduction_is_deterministic_and_healthy():
    from . import metrics
    a = metrics.measure()
    b = metrics.measure()
    # byte-identical run to run (the whole point of a deterministic workload).
    assert a == b
    # ZERO continuity violations across the whole workload — the blog's core
    # correctness invariant, on Stratoclave's decision core.
    assert a["continuity_violations"] == 0
    # SAAR meaningfully reduces switches (blog: 79.29%); our synthetic workload
    # is self-consistent, so pin a strong lower bound rather than an exact %.
    assert a["switch_reduction_pct"] >= 75.0
    assert a["saar_switches"] < a["no_saar_switches"]
    # SAAR still MOVES when it should (not a degenerate hard-sticky pin): drift
    # and idle-reset turns are present in the reason histogram.
    assert a["reason_histogram"].get("drift", 0) > 0
    assert a["reason_histogram"].get("reset", 0) > 0
    assert a["hard_locks"] > 0  # tool loops were locked
