"""Canonical catalogue of the vLLM Session-Aware Agentic Routing (SAAR) blog's
claims, and a deterministic turn-driver that exercises Stratoclave's SAAR
decision core against each one.

Source: https://vllm.ai/blog/2026-06-02-session-aware-agentic-routing

WHY THIS EXISTS
---------------
The blog makes nine concrete behavioural claims. Stratoclave re-implements the
SUBSET of SAAR that fits a credit/billing gateway (the decision core in
`mvp.routing.saar`), NOT vLLM's serving-side router. This module pins, in one
place, exactly which blog claims Stratoclave covers, which it deliberately does
NOT (and why), so "do we verify the blog scenarios?" has a machine-checked
answer instead of a vibe. Each scenario carries a `coverage` field the test
suite asserts against, so an honest gap (e.g. provider-state lock is a P1
concern — the phase constant exists but nothing SETS it) is visible, never
silently claimed as covered.

The driver is deliberately built on the PURE decision surface (`saar.decide`,
`saar.next_phase_after_turn`, `saar.request_has_tool_result`) with an in-memory
SessionMemory — no DynamoDB, no network — so a scenario is a fully deterministic
sequence of turns whose committed models the driver picks by a fixed policy.
That determinism is what lets the metrics (switch-reduction %, violations) be
reproducible run to run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from mvp.routing import saar

# ---------------------------------------------------------------------------
# coverage taxonomy
# ---------------------------------------------------------------------------

COVERED = "covered"              # implemented + asserted here
COVERED_ELSEWHERE = "covered-elsewhere"  # implemented; unit/formal test owns it, referenced here
NOT_IMPLEMENTED = "not-implemented"      # blog claim Stratoclave deliberately does NOT implement (P1/out-of-scope)


@dataclass(frozen=True)
class BlogScenario:
    id: str
    claim: str                    # what the blog asserts
    stratoclave_behavior: str     # what Stratoclave's SAAR does (or why it doesn't)
    coverage: str
    owner_test: Optional[str] = None  # where the assertion lives (this file unless noted)


# The nine claims, verbatim-faithful to the blog, mapped to Stratoclave.
SCENARIOS: list[BlogScenario] = [
    BlogScenario(
        id="tool-loop-hard-lock",
        claim="A tool result must return to the same physical model that emitted "
              "the tool_use; the blog eliminated 3,404 tool-loop violations.",
        stratoclave_behavior="Phase.TOOL_LOOP + request_has_tool_result => "
              "decide() returns hard_model=last_physical_model (cascade disabled).",
        coverage=COVERED,
    ),
    BlogScenario(
        id="idle-timeout-reset",
        claim="After a configured idle boundary (300s) continuity pressure decays "
              "and the session may reselect.",
        stratoclave_behavior="decide(): idle > idle_reset_seconds => phase RESET, "
              "no opinion, cache evidence discarded (stale=True).",
        coverage=COVERED,
    ),
    BlogScenario(
        id="decision-drift-reset",
        claim="When the matched routing decision changes (code-edit -> synthesis) "
              "the router reopens selection despite session continuity.",
        stratoclave_behavior="decide(): normal phase + matched_decision changed => "
              "reason 'drift', no opinion (cascade reselects).",
        coverage=COVERED,
    ),
    BlogScenario(
        id="sticky-not-just-sticky",
        claim="SAAR is not just sticky sessions: sticky cuts switches 98.65% at "
              "-0.1433 quality; SAAR cuts 79.29% at -0.0453 quality by reselecting "
              "after the task changes.",
        stratoclave_behavior="Sticky is a SOFT preference (prefer_model), and drift/"
              "idle reset RE-OPEN selection — so Stratoclave's SAAR reduces switches "
              "yet still moves when the task changes, unlike a pure hard sticky pin.",
        coverage=COVERED,
    ),
    BlogScenario(
        id="prefix-cache-checkout-pricing",
        claim="Switching away from a warm session forfeits prefix-cache locality; "
              "the cost is the gap between normal-input and cached-input price, "
              "asymmetric by model, weighted (prefix_cache_weight 0.20).",
        stratoclave_behavior="checkout delta = (input - cache_read) priced in "
              "micro-USD, never negative, zero without warm-prefix evidence.",
        coverage=COVERED_ELSEWHERE,
        owner_test="tests/test_saar.py::test_checkout_delta_* + "
                   "tests/test_saar_formal.py::test_inv2_*",
    ),
    BlogScenario(
        id="router-memory-scope",
        claim="Router memory stores last physical model, matched decision, phase, "
              "switch count, idle time, cache evidence, replay metadata — NOT "
              "conversation/retrieval memory or user profiles.",
        stratoclave_behavior="SessionMemory carries exactly those fields and no "
              "message content; store round-trips them, tenant-isolated.",
        coverage=COVERED_ELSEWHERE,
        owner_test="tests/test_saar.py::test_store_roundtrip + test_store_tenant_isolation",
    ),
    BlogScenario(
        id="fault-recovery",
        claim="Under HTTP 503 injection, sessions recover 100% without losing "
              "routing invariants (32 sessions, 168 repeated failures, 0 continuity "
              "violations).",
        stratoclave_behavior="Every memory read/write is fail-open: a 503/timeout "
              "on load => memory=None => cascade (no crash); the hard locks are "
              "re-derived from THIS request's content, so a lost read never breaks "
              "the tool-loop invariant.",
        coverage=COVERED,
    ),
    BlogScenario(
        id="live-zero-violations",
        claim="Live serving showed 0 continuity violations across 2,896 requests "
              "over balanced/stateful/idle workloads.",
        stratoclave_behavior="A deterministic multi-session workload driven through "
              "the decision core yields 0 tool-loop violations (a tool return never "
              "lands on a model other than the one that opened the loop).",
        coverage=COVERED,
    ),
    BlogScenario(
        id="provider-state-lock",
        claim="Requests carrying non-portable continuation state (a response id) "
              "are locked to their backend; the blog eliminated 432 provider-state "
              "violations.",
        stratoclave_behavior="A Responses request carrying previous_response_id "
              "sets request_has_provider_state; a turn that minted a response id "
              "persists Phase.PROVIDER_STATE via next_phase_after_turn; decide() "
              "then hard-locks the continuation back to its origin model (peer of "
              "the tool-loop lock, checked BEFORE idle reset so a still-referenced "
              "continuation is never reset away). Wired into the /v1/responses "
              "handler (non-stream + stream).",
        coverage=COVERED,
    ),
]


def by_id(scenario_id: str) -> BlogScenario:
    for s in SCENARIOS:
        if s.id == scenario_id:
            return s
    raise KeyError(scenario_id)


# ---------------------------------------------------------------------------
# deterministic turn driver (pure decision core, in-memory session)
# ---------------------------------------------------------------------------


@dataclass
class Turn:
    """One agent turn fed to the driver."""
    has_tool_result: bool = False      # this request returns a tool output
    emits_tool_use: bool = False       # the response asks for a tool (=> next turn locks)
    # provider-state (Responses continuation). `emitted_response_id`: the id THIS
    # turn's response mints (armed for the next turn). `references_response_id`:
    # the previous_response_id THIS request sends. A lock fires only when the
    # referenced id EXACTLY equals the id the prior turn minted (verified).
    emitted_response_id: Optional[str] = None
    references_response_id: Optional[str] = None
    matched_decision: Optional[str] = None  # routing-decision label (for drift)
    at_epoch: int = 0                  # wall-clock of this turn (for idle reset)
    # The model the cascade WOULD pick this turn if SAAR had no opinion. This is
    # the counterfactual "no-SAAR" choice; the driver uses it to (a) commit when
    # SAAR is silent and (b) compute the switch-reduction metric vs a SAAR run.
    cascade_choice: str = "opus"
    # Metrics-only hint: when True, a custom commit_policy may treat the warm
    # model as unavailable this turn (simulating a breaker cap / quota) so a soft
    # preference falls through to the cascade. Ignored by the default policy and
    # by hard locks (which always win). Lets the metrics workload avoid a
    # degenerate 100%-sticky result.
    force_switch: bool = False


@dataclass
class TurnResult:
    turn_index: int
    committed_model: str
    reason: str
    phase_in: str
    phase_out: str
    hard_locked: bool
    switched: bool
    violation: bool          # a tool return committed to a model != the loop's model


@dataclass
class DriveOutcome:
    results: list[TurnResult] = field(default_factory=list)

    @property
    def switches(self) -> int:
        return sum(1 for r in self.results if r.switched)

    @property
    def violations(self) -> int:
        return sum(1 for r in self.results if r.violation)

    @property
    def committed(self) -> list[str]:
        return [r.committed_model for r in self.results]


def drive_session(
    turns: list[Turn],
    *,
    idle_reset_seconds: int = 300,
    commit_policy: Optional[Callable[[Turn, saar.SaarDecision], str]] = None,
) -> DriveOutcome:
    """Run a sequence of turns through the PURE SAAR decision core with an
    in-memory session, mirroring what saar_pre_reserve/saar_post_settle do around
    the reserve — but with no I/O so it is fully deterministic.

    Commit policy (how the turn's committed model is chosen):
      * a hard lock  => the locked model, ALWAYS (correctness);
      * a soft prefer => the preferred model (the cascade would head there and,
        in these scenarios, it is always servable);
      * otherwise    => the turn's `cascade_choice` (SAAR had no opinion).
    A custom `commit_policy` can override the non-hard case to simulate a cascade
    that refuses the preference (availability), etc.
    """
    mem: Optional[saar.SessionMemory] = None
    out = DriveOutcome()

    for i, t in enumerate(turns):
        decision = saar.decide(
            mem=mem,
            now_epoch=t.at_epoch,
            request_has_tool_result=t.has_tool_result,
            request_provider_state_id=t.references_response_id,
            matched_decision=t.matched_decision,
            idle_reset_seconds=idle_reset_seconds,
        )

        # choose the committed model
        if decision.hard_model:
            committed = decision.hard_model
        elif commit_policy is not None:
            committed = commit_policy(t, decision)
        elif decision.prefer_model:
            committed = decision.prefer_model
        else:
            committed = t.cascade_choice

        prev_model = mem.last_physical_model if mem else None
        switched = bool(prev_model) and committed != prev_model

        # a violation: a continuity-bearing request committed to a model OTHER
        # than the one holding the continuity. Two forms:
        #  * a tool result while in tool-loop phase went to a different model;
        #  * a provider-state continuation reference while in provider-state phase
        #    went to a different (non-origin) backend.
        tool_loop_violation = (
            t.has_tool_result
            and mem is not None
            and mem.phase == saar.Phase.TOOL_LOOP
            and committed != mem.last_physical_model
        )
        # provider-state violation: the request references EXACTLY the id the
        # prior turn minted (a real, verified continuation), the session is in
        # provider-state phase, yet the commit went to a different backend.
        provider_state_violation = (
            mem is not None
            and mem.phase == saar.Phase.PROVIDER_STATE
            and t.references_response_id is not None
            and mem.minted_response_id is not None
            and t.references_response_id == mem.minted_response_id
            and committed != mem.last_physical_model
        )
        violation = tool_loop_violation or provider_state_violation

        phase_out = saar.next_phase_after_turn(
            response_had_tool_use=t.emits_tool_use,
            request_had_tool_result=t.has_tool_result,
            response_emitted_provider_state=bool(t.emitted_response_id),
        )
        out.results.append(TurnResult(
            turn_index=i,
            committed_model=committed,
            reason=decision.reason,
            phase_in=(mem.phase if mem else saar.Phase.NORMAL),
            phase_out=phase_out,
            hard_locked=bool(decision.hard_model),
            switched=switched,
            violation=violation,
        ))

        # persist (in-memory) exactly as saar_post_settle would, storing the id
        # this turn minted so the NEXT turn can only lock by echoing it back.
        mem = saar.SessionMemory(
            last_physical_model=committed,
            phase=phase_out,
            matched_decision=t.matched_decision,
            switch_count=(mem.switch_count if mem else 0) + (1 if switched else 0),
            turn_count=(mem.turn_count if mem else 0) + 1,
            last_turn_at=t.at_epoch,
            warm_prefix_tokens=0,
            minted_response_id=t.emitted_response_id,
        )

    return out


def drive_no_saar(turns: list[Turn]) -> DriveOutcome:
    """Counterfactual: the same turns with NO session awareness — every turn
    takes its cascade_choice independently. Used to compute switch reduction."""
    out = DriveOutcome()
    prev = None
    for i, t in enumerate(turns):
        committed = t.cascade_choice
        switched = bool(prev) and committed != prev
        out.results.append(TurnResult(
            turn_index=i, committed_model=committed, reason="no-saar",
            phase_in=saar.Phase.NORMAL, phase_out=saar.Phase.NORMAL,
            hard_locked=False, switched=switched, violation=False,
        ))
        prev = committed
    return out
