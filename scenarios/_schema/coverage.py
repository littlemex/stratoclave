"""Machine-readable coverage aggregation for the Stratoclave workshop scenarios.

Each scenario ships a `coverage.yaml` naming, per workshop STEP, whether the
capability that step exercises is:

    covered              — runs today on shipped, all-green artifacts
    covered-elsewhere    — shipped; a unit/formal test owns the assertion
    not-implemented      — a genuine gap; MUST carry an `issue:` link (roadmap)
    user-responsibility  — out of scope by the responsibility boundary
                           (measurement / eval / availability / backend fit)

This module is PURE (parse + aggregate + render + lint). It has no side effects
beyond reading the scenario tree; the CI test (backend/tests/
test_scenarios_coverage.py) calls `render_matrix()` to regenerate COVERAGE.md and
`lint()` to enforce the two honesty rules:

  RULE 1  every `not-implemented` step carries a tracking `issue:` — so the
          coverage matrix IS the implementation roadmap, not a vibe.
  RULE 2  a step whose prose CLAIMS a measurement ("measures:" set truthy) must
          NOT be `not-implemented` or `user-responsibility` — you cannot say a
          step measures something the gateway cannot yet measure. This is the
          machine guard against over-claiming (the same posture as the demo
          doc-number pinning test).

The taxonomy is the SAAR blog_scenarios.py coverage concept, promoted to a
cross-scenario, cross-axis schema.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

import yaml

COVERED = "covered"
COVERED_ELSEWHERE = "covered-elsewhere"
NOT_IMPLEMENTED = "not-implemented"
USER_RESPONSIBILITY = "user-responsibility"

VALID_STATES = frozenset(
    {COVERED, COVERED_ELSEWHERE, NOT_IMPLEMENTED, USER_RESPONSIBILITY})

# States that mean "this step does NOT run on shipped code here". A step that
# claims to MEASURE something must not be one of these (RULE 2).
_NON_RUNNING = frozenset({NOT_IMPLEMENTED, USER_RESPONSIBILITY})


@dataclass(frozen=True)
class Step:
    id: str
    axis: str                 # cost | perf | quality | admin | usage
    capability: str           # the gateway capability the step exercises
    state: str
    note: str = ""
    issue: Optional[str] = None
    measures: bool = False    # does the step's prose claim a measurement?
    # `evidence` is ORTHOGONAL to `state` (Fable live-verify review): state is what
    # the scenario is DESIGNED to cover; evidence records that a LIVE run actually
    # exercised it. A dict like {mode: live, date, region, n, run_id}. Never a 5th
    # state — live-ness is a separate axis from coverage design.
    evidence: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    audience: str             # "admin" | "user"
    path: str
    steps: list[Step] = field(default_factory=list)


class CoverageError(ValueError):
    """A coverage.yaml is malformed or violates an honesty rule."""


def _parse_scenario(path: str, raw: dict[str, Any]) -> Scenario:
    try:
        sid = str(raw["id"])
        title = str(raw["title"])
        audience = str(raw["audience"])
    except KeyError as e:
        raise CoverageError(f"{path}: missing top-level key {e}") from e
    if audience not in ("admin", "user"):
        raise CoverageError(f"{path}: audience must be admin|user, got {audience!r}")
    steps: list[Step] = []
    for i, s in enumerate(raw.get("steps") or []):
        try:
            state = str(s["state"])
        except KeyError as e:
            raise CoverageError(f"{path}: step {i} missing 'state'") from e
        if state not in VALID_STATES:
            raise CoverageError(
                f"{path}: step {s.get('id', i)} has invalid state {state!r} "
                f"(valid: {sorted(VALID_STATES)})")
        ev = s.get("evidence")
        if ev is not None and not isinstance(ev, dict):
            raise CoverageError(
                f"{path}: step {s.get('id', i)} evidence must be a mapping")
        steps.append(Step(
            id=str(s.get("id", f"step-{i}")),
            axis=str(s.get("axis", "")),
            capability=str(s.get("capability", "")),
            state=state,
            note=str(s.get("note", "")),
            issue=(str(s["issue"]) if s.get("issue") else None),
            measures=bool(s.get("measures", False)),
            evidence=ev,
        ))
    if not steps:
        raise CoverageError(f"{path}: scenario {sid!r} has no steps")
    return Scenario(id=sid, title=title, audience=audience, path=path, steps=steps)


def scenarios_root() -> str:
    """The scenarios/ directory this module lives under."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_scenarios(root: Optional[str] = None) -> list[Scenario]:
    """Find and parse every scenarios/**/coverage.yaml (skips _schema/)."""
    root = root or scenarios_root()
    found: list[Scenario] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # never descend into the schema/template directory
        dirnames[:] = [d for d in dirnames if d != "_schema"]
        if "coverage.yaml" in filenames:
            full = os.path.join(dirpath, "coverage.yaml")
            with open(full) as fh:
                raw = yaml.safe_load(fh) or {}
            rel = os.path.relpath(dirpath, root)
            found.append(_parse_scenario(rel, raw))
    found.sort(key=lambda s: s.path)
    return found


def lint(scenarios: list[Scenario]) -> list[str]:
    """Return a list of honesty-rule violations (empty == clean)."""
    problems: list[str] = []
    seen_ids: set[str] = set()
    for sc in scenarios:
        if sc.id in seen_ids:
            problems.append(f"{sc.path}: duplicate scenario id {sc.id!r}")
        seen_ids.add(sc.id)
        for st in sc.steps:
            # RULE 1: not-implemented MUST carry a tracking issue.
            if st.state == NOT_IMPLEMENTED and not st.issue:
                problems.append(
                    f"{sc.path}/{st.id}: state 'not-implemented' requires an "
                    f"'issue:' link (the gap must be on the roadmap)")
            # RULE 2: a step that claims to MEASURE must actually run on shipped code.
            if st.measures and st.state in _NON_RUNNING:
                problems.append(
                    f"{sc.path}/{st.id}: step is marked measures:true but state is "
                    f"{st.state!r} — cannot claim a measurement the gateway does not "
                    f"run. Set measures:false (and describe it as a gap) or provide a "
                    f"covered capability.")
    return problems


def _counts(scenarios: list[Scenario]) -> dict[str, int]:
    c: dict[str, int] = {s: 0 for s in VALID_STATES}
    for sc in scenarios:
        for st in sc.steps:
            c[st.state] += 1
    return c


def render_matrix(scenarios: list[Scenario]) -> str:
    """Render COVERAGE.md — the auto-generated capability x scenario x state table.
    DO NOT hand-edit the output file; regenerate via the CI test."""
    counts = _counts(scenarios)
    total = sum(counts.values())
    lines: list[str] = []
    lines.append("<!-- AUTO-GENERATED by scenarios/_schema/coverage.py via "
                 "backend/tests/test_scenarios_coverage.py. DO NOT EDIT BY HAND. -->")
    lines.append("")
    lines.append("# Workshop coverage matrix")
    lines.append("")
    lines.append("What each workshop scenario exercises, and — honestly — which "
                 "steps run on shipped code today versus which are gaps on the "
                 "roadmap or the user's responsibility. This file is the workshop's "
                 "way of turning \"try to measure X\" into a machine-checked "
                 "implementation to-do list.")
    lines.append("")
    lines.append("## Totals")
    lines.append("")
    lines.append("| State | Steps | Meaning |")
    lines.append("|---|---|---|")
    lines.append(f"| `covered` | {counts[COVERED]} | runs today on shipped, "
                 "all-green artifacts |")
    lines.append(f"| `covered-elsewhere` | {counts[COVERED_ELSEWHERE]} | shipped; "
                 "a unit/formal test owns the assertion |")
    lines.append(f"| `not-implemented` | {counts[NOT_IMPLEMENTED]} | a gap — carries "
                 "a tracking issue (see below) |")
    lines.append(f"| `user-responsibility` | {counts[USER_RESPONSIBILITY]} | out of "
                 "scope by the responsibility boundary |")
    lines.append(f"| **total** | **{total}** | |")
    lines.append("")
    lines.append("## By scenario")
    lines.append("")
    for sc in scenarios:
        lines.append(f"### `{sc.path}` — {sc.title} ({sc.audience})")
        lines.append("")
        lines.append("| Step | Axis | Capability | State | Evidence | Notes |")
        lines.append("|---|---|---|---|---|---|")
        for st in sc.steps:
            # collapse any internal newlines (folded YAML notes) so the markdown
            # table cell stays on one line.
            note = " ".join(st.note.split())
            if st.issue:
                note = (note + " " if note else "") + f"({st.issue})"
            ev = "—"
            if st.evidence:
                ev = (f"live {st.evidence.get('date', '?')} "
                      f"(N={st.evidence.get('n', '?')}, run={st.evidence.get('run_id', '?')})")
            lines.append(
                f"| {st.id} | {st.axis} | {st.capability} | `{st.state}` | {ev} | {note} |")
        lines.append("")
    # Roadmap: every not-implemented gap, grouped by capability so a gap hit by
    # multiple scenarios sorts to the top (highest-leverage to implement).
    gaps: dict[str, list[str]] = {}
    for sc in scenarios:
        for st in sc.steps:
            if st.state == NOT_IMPLEMENTED:
                gaps.setdefault(st.capability, []).append(f"{sc.path}/{st.id}")
    lines.append("## Roadmap (from `not-implemented` gaps)")
    lines.append("")
    if not gaps:
        lines.append("_No open gaps._")
    else:
        lines.append("Gaps ranked by how many scenarios hit them "
                     "(shared gaps are highest-leverage to implement first).")
        lines.append("")
        lines.append("| Capability | Hit by | Scenarios |")
        lines.append("|---|---|---|")
        for cap, hits in sorted(gaps.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            lines.append(f"| {cap} | {len(hits)} | {', '.join(hits)} |")
    lines.append("")
    return "\n".join(lines)
