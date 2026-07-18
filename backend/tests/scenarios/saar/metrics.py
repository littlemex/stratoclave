"""Deterministic SAAR metrics harness — reproduces the blog's headline figures
(switch-reduction %, continuity violations) on a fixed synthetic workload so the
numbers are the SAME every run and can be logged/gated.

This is NOT a claim to match the blog's exact 79.29% (that figure is specific to
the blog's 21,600-turn trace on vLLM's serving router). It is a reproducible,
self-consistent measurement of Stratoclave's OWN decision core: SAAR vs a no-SAAR
cascade over the same turns, emitted as one structured JSON line so a cron/CI run
leaves an auditable record and a regression (violations > 0, or reduction
collapsing) is visible.

Run:  python -m tests.scenarios.saar.metrics        # prints one JSON line
      pytest -m saar_scenario                        # asserts the invariants
"""
from __future__ import annotations

import json

from . import blog_scenarios as bs


def build_workload(*, n_sessions: int = 200, turns_per: int = 12) -> list[list[bs.Turn]]:
    """A fixed, seedless multi-session workload. Deterministic by construction
    (no RNG): session `s` and turn `k` fully determine the turn's shape, so the
    metrics are byte-reproducible run to run (the property the blog's '21,600
    deterministic turns' relies on).

    Each session mixes the blog's regimes, but NEVER injects an idle gap in the
    middle of an open tool loop (a 300s+ gap legitimately ends the session, so a
    reselection after it is CORRECT, not a violation — mixing the two would be a
    malformed workload, not a SAAR bug):
      * tool loops in the first three-quarters: turn 3k opens (tool_use), turn
        3k+1 returns the result while the cascade tries to pull the model away;
      * a mid-session task change (drift) at the halfway point;
      * a single idle gap ONLY at the boundary into the last quarter, and the
        last quarter runs plain (no tool loop) so the reset is unambiguous.
    """
    models = ["opus", "sonnet", "haiku"]
    sessions: list[list[bs.Turn]] = []
    last_q = (3 * turns_per) // 4
    for s in range(n_sessions):
        base = 1_000 + s * 100_000
        turns: list[bs.Turn] = []
        clock = base
        for k in range(turns_per):
            # cascade's independent (flapping) choice — rotates so no-SAAR churns.
            cascade = models[(s + k) % len(models)]
            decision_label = "code" if k < turns_per // 2 else "synth"  # drift at halfway
            # exactly ONE idle gap, at the boundary into the last quarter.
            clock += 400 if k == last_q else 1
            in_last_quarter = k >= last_q
            # tool loop only BEFORE the last quarter, so an idle gap never splits
            # an open loop.
            in_tool_loop_open = (not in_last_quarter) and (k % 3 == 0)
            in_tool_loop_return = (not in_last_quarter) and (k % 3 == 1)
            # ~1/4 of turns: the warm model is 'unavailable', so a soft prefer
            # falls through to the cascade (a real switch). Deterministic: every
            # 4th turn index. Never on a tool-loop return (a hard lock ignores it).
            force_switch = (k % 4 == 3) and not in_tool_loop_return
            turns.append(bs.Turn(
                has_tool_result=in_tool_loop_return,
                emits_tool_use=in_tool_loop_open,
                matched_decision=decision_label,
                at_epoch=clock,
                cascade_choice=cascade,
                force_switch=force_switch,
            ))
        sessions.append(turns)
    return sessions


def measure(*, n_sessions: int = 200, turns_per: int = 12) -> dict:
    sessions = build_workload(n_sessions=n_sessions, turns_per=turns_per)
    total_turns = 0
    saar_switches = 0
    no_saar_switches = 0
    violations = 0
    hard_locks = 0
    reason_hist: dict[str, int] = {}

    def _commit_policy(t: bs.Turn, d) -> str:
        """Realistic cascade: honor SAAR's soft preference EXCEPT when the warm
        model is 'unavailable' this turn (simulated by the turn's own
        `force_switch` flag ~1/4 of eligible turns). Models the blog's reality
        that sticky doesn't win every turn, so switch reduction is high but not a
        degenerate 100%. Hard locks are handled by the driver before this runs,
        so refusing a preference here can never break a tool loop."""
        if d.prefer_model and not t.force_switch:
            return d.prefer_model
        return t.cascade_choice

    for turns in sessions:
        saar_out = bs.drive_session(turns, commit_policy=_commit_policy)
        no_saar = bs.drive_no_saar(turns)
        total_turns += len(turns)
        saar_switches += saar_out.switches
        no_saar_switches += no_saar.switches
        violations += saar_out.violations
        for r in saar_out.results:
            if r.hard_locked:
                hard_locks += 1
            reason_hist[r.reason] = reason_hist.get(r.reason, 0) + 1

    reduction_pct = (
        round(100.0 * (no_saar_switches - saar_switches) / no_saar_switches, 2)
        if no_saar_switches else 0.0
    )
    return {
        "metric": "saar_scenario_reproduction",
        "sessions": n_sessions,
        "turns_per_session": turns_per,
        "total_turns": total_turns,
        "no_saar_switches": no_saar_switches,
        "saar_switches": saar_switches,
        "switch_reduction_pct": reduction_pct,
        "continuity_violations": violations,
        "hard_locks": hard_locks,
        "reason_histogram": reason_hist,
    }


def main() -> int:
    report = measure()
    # one structured JSON line — greppable in CI logs, parseable by a collector.
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
