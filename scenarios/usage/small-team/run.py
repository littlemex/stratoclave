"""Workshop scenario: a small team on one shared budget pool.

Runnable, deterministic, offline-first. This driver walks the three axes a team
lead cares about and — honestly — shows which run today and which hit a gap:

  COST     (runs today): fold the team's VSR-acted traffic into a Savings
           Certificate with the REAL engine (mvp.learning.savings) over a tiny
           checked-in workload. Reuses the exact offline path the demo uses.
  PERF     (GAP): try to report TTFT / TPOT per request. The gateway does not
           emit token-timing today — only `ledger_transact_latency` (the billing
           write). We surface THAT, name the gap, and point at the tracking issue.
  QUALITY  (partial): score a tiny, deterministic, EXACT-MATCH task set to show
           the *shape* of an honest accuracy check — conservative (ambiguous =
           not-correct), N stamped, no judge model, no similarity. The eval TAP
           that would feed this from real traffic is a GAP.

Nothing is written anywhere; every number is a pure fold over checked-in data, so
the scenario doc's figures can be pinned by CI and can never silently drift.

    python scenarios/usage/small-team/run.py            # human-readable
    python scenarios/usage/small-team/run.py --json     # raw JSON

Engine code is UNCHANGED. This is glue over shipped seams.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Make backend/ importable whether run as a script or a module.
_ROOT = Path(__file__).resolve().parents[3]
_BACKEND = _ROOT / "backend"
if _BACKEND.is_dir() and str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from mvp.learning import savings as sv          # noqa: E402
from mvp.pricing import BUILTIN_VERSION         # noqa: E402

_WORKLOAD = Path(__file__).with_name("team_workload.jsonl")
_EVALSET = Path(__file__).with_name("mini_eval.jsonl")

# The tracking gaps this scenario surfaces (also referenced in coverage.yaml,
# kept in sync by the coverage lint). They point at scenarios/GAPS.md, the home
# for capabilities the workshops surface as missing.
ISSUE_TTFT = "scenarios/GAPS.md#perf-token-timing"
ISSUE_EVAL_TAP = "scenarios/GAPS.md#quality-eval-tap"


# ---------------------------------------------------------------------------
# COST axis — runs today on the real engine.
# ---------------------------------------------------------------------------
def cost_axis() -> dict:
    price = sv._default_pricer()
    resolve = sv._default_resolver()
    rows = []
    with _WORKLOAD.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            billed = resolve(str(r.get("billed_model_id") or ""))
            if billed and r.get("input_tokens") is not None:
                # bill is recomputed by the REAL pricer — never hand-authored.
                r["cost_microusd"] = int(price(
                    billed["pricing_key"], int(r["input_tokens"]), int(r["output_tokens"])))
            rows.append(r)
    savings = sv.summarize_savings(rows, price=price, resolve=resolve)
    return {
        "rate_version": BUILTIN_VERSION,
        "traffic": "synthetic",
        "net_saving_microusd": savings["net_saving_microusd"],
        "priced_request_count": savings["priced_request_count"],
        "decomposition": savings["decomposition"],
        "potential_net_microusd": savings["potential"]["net_saving_microusd"],
        "class_counts": savings["class_counts"],
        "quality_measured": savings["quality"]["measured"],
    }


# ---------------------------------------------------------------------------
# PERF axis — GAP. The gateway does not emit token timing today.
# ---------------------------------------------------------------------------
def perf_axis() -> dict:
    """We WANT TTFT/TPOT per request. Today the only latency the gateway emits is
    `ledger_transact_latency` (the billing write), which is NOT token timing. This
    function reports that honestly rather than inventing a TTFT number."""
    return {
        "wanted": ["ttft_ms", "tpot_ms"],
        "emitted_today": ["ledger_transact_latency (billing write, not token timing)"],
        "ttft_ms": None,      # deliberately None — not measured
        "tpot_ms": None,      # deliberately None — not measured
        "gap": "not-implemented",
        "issue": ISSUE_TTFT,
        "note": ("The streaming path yields frames but does not timestamp the "
                 "first token or inter-token gaps. A perf metric here needs a "
                 "token-timing hook on the stream — see the issue."),
    }


# ---------------------------------------------------------------------------
# QUALITY axis — a tiny EXACT-MATCH scorer (honest by construction).
# ---------------------------------------------------------------------------
def score_exact_match(records: list[dict]) -> dict:
    """Pure, conservative accuracy fold over a deterministic task set. `expected`
    is the exact string; `answer` is what a model returned (checked-in here so the
    scenario is reproducible offline). Conservative bias: a blank/ambiguous answer
    is counted NOT correct, never given the benefit of the doubt. N and the method
    are stamped so this can never be read as a quality benchmark."""
    n = len(records)
    correct = 0
    graded = 0
    for r in records:
        ans = (r.get("answer") or "").strip()
        exp = (r.get("expected") or "").strip()
        if not exp:
            continue                    # not a gradable item
        graded += 1
        # exact match only; ambiguity falls to NOT correct (conservative).
        if ans and ans == exp:
            correct += 1
    return {
        "n": n,
        "graded": graded,
        "correct": correct,
        "accuracy": (correct / graded) if graded else None,
        "method": "exact-match, conservative (ambiguous=not-correct)",
        "caveat": (f"workshop task set, N={graded} — a mechanism demo, NOT a "
                   "quality benchmark. No judge model, no similarity score."),
        "tap_gap": {
            "gap": "not-implemented",
            "issue": ISSUE_EVAL_TAP,
            "note": ("This scores a checked-in task set. Feeding it from a team's "
                     "REAL request/response traffic needs an eval tap (JSONL of "
                     "prompt+response by span_id) the gateway does not emit yet."),
        },
    }


def quality_axis() -> dict:
    records = []
    with _EVALSET.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return score_exact_match(records)


def build_report() -> dict:
    return {
        "scenario": "usage/small-team",
        "cost": cost_axis(),
        "perf": perf_axis(),
        "quality": quality_axis(),
    }


def _usd(micro: int) -> str:
    n = abs(int(micro))
    return f"{'-' if micro < 0 else ''}${n // 1_000_000}.{n % 1_000_000:06d}"


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    report = build_report()
    if "--json" in argv:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0

    c = report["cost"]
    print("=== Workshop: a small team on one shared budget pool ===")
    print("    *** SYNTHETIC workshop data — a mechanism demo, not audited numbers ***")
    print()
    print("[COST]  (runs today — real Savings Certificate engine)")
    print(f"  rate version:            {c['rate_version']}")
    print(f"  priced requests (base):  {c['priced_request_count']}")
    print(f"  NET saving if followed:  {_usd(c['net_saving_microusd'])}")
    print(f"    (+ cheaper-if-followed {_usd(c['decomposition']['positive_deltas_microusd'])}"
          f" / - dearer {_usd(c['decomposition']['negative_deltas_microusd'])})")
    print(f"  potential (advice only): {_usd(c['potential_net_microusd'])} (never in headline)")
    print(f"  request classes:         {c['class_counts']}")
    print(f"  quality measured:        {c['quality_measured']}")
    print()

    p = report["perf"]
    print("[PERF]  (GAP — token timing not emitted today)")
    print(f"  wanted:        {', '.join(p['wanted'])}")
    print(f"  emitted today: {', '.join(p['emitted_today'])}")
    print(f"  TTFT / TPOT:   {p['ttft_ms']} / {p['tpot_ms']}  <- not measured")
    print(f"  gap:           {p['gap']} ({p['issue']})")
    print()

    q = report["quality"]
    print("[QUALITY]  (partial — exact-match scorer runs; the eval tap is a GAP)")
    acc = q["accuracy"]
    acc_s = f"{acc:.0%}" if acc is not None else "n/a"
    print(f"  exact-match accuracy:  {q['correct']}/{q['graded']} = {acc_s}")
    print(f"  method:                {q['method']}")
    print(f"  caveat:                {q['caveat']}")
    print(f"  tap gap:               {q['tap_gap']['gap']} ({q['tap_gap']['issue']})")
    print()
    print("See coverage.yaml / scenarios/COVERAGE.md for the machine-checked "
          "capability map. The two gaps above are the workshop's output: the next "
          "features to implement.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
