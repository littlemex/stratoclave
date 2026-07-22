<!-- Last updated: 2026-07-22. Numbers here are pinned to run.py output by
     backend/tests/test_scenarios_coverage.py — regenerate, don't hand-edit them. -->

# A small team on one shared budget pool

**Audience:** user (a team lead)
**Runs on:** shipped, all-green artifacts only (offline-first).

## What you'll see

A six-request day for a three-person team (`acme-team`) sharing one budget pool.
You walk the three axes a team lead asks about — **cost, performance, accuracy** —
and see, honestly, which the gateway answers today and which it does not:

- **Cost** runs fully today: the real Savings Certificate engine tells you what
  the team's routing advice was worth (with the escalation loss subtracted, not
  hidden).
- **Performance** hits a **gap**: you try to read TTFT/TPOT and find the gateway
  emits only the billing-write latency. That gap is the lesson.
- **Accuracy** runs a tiny exact-match scorer to show the honest *shape* of a
  quality check — but feeding it from real traffic is a **gap**.

The two gaps are not a failure of the workshop; they are its output — the next
features to build, made machine-visible in [`COVERAGE.md`](../../COVERAGE.md).

## Prerequisites

- Python, this repo, no cloud and no network (the whole scenario is a pure fold
  over checked-in data).
- **Responsibility boundary:** measurement, evaluation, availability targets, and
  backend fit are the operator's responsibility. This scenario provides the
  *mechanism* — a deterministic script, a metric definition, a pure scoring fold —
  not an audited number for your workload.

## Steps

Run it:

```bash
python scenarios/usage/small-team/run.py           # human-readable
python scenarios/usage/small-team/run.py --json     # raw JSON
```

### 1. Cost — what the routing advice was worth (runs today)

The team's six requests fold into a Savings Certificate. Expected output:

```
[COST]  (runs today — real Savings Certificate engine)
  rate version:            builtin
  priced requests (base):  3
  NET saving if followed:  $0.030000
    (+ cheaper-if-followed $0.086000 / - dearer $0.056000)
  potential (advice only): $0.064000 (never in headline)
  request classes:         {'counterfactual': 4, 'followed': 1, 'no_suggestion': 1}
  quality measured:        False
```

Read it honestly: the **net** ($0.030) is what the *enacted* advice saved — the
cheaper-if-followed total ($0.086) minus a real **escalation loss** ($0.056)
where the router advised the *dearer* model. The shadow-only advice ($0.064
`potential`) is kept out of the headline. `quality measured: False` — the cost is
proven; the quality is not claimed.

### 2. Performance — try to read TTFT/TPOT (hits a gap)

```
[PERF]  (GAP — token timing not emitted today)
  TTFT / TPOT:   None / None  <- not measured
  gap:           not-implemented (scenarios/GAPS.md#perf-token-timing)
```

The gateway emits `ledger_transact_latency` (the billing write) but does not
timestamp the first token or inter-token gaps. There is no honest TTFT number to
print, so the scenario prints none and names the gap. See
[`GAPS.md`](../../GAPS.md#perf-token-timing).

### 3. Accuracy — a tiny exact-match scorer (mechanism runs; the tap is a gap)

```
[QUALITY]  (partial — exact-match scorer runs; the eval tap is a GAP)
  exact-match accuracy:  8/10 = 80%
  method:                exact-match, conservative (ambiguous=not-correct)
  tap gap:               not-implemented (scenarios/GAPS.md#quality-eval-tap)
```

Ten deterministic tasks (arithmetic, extraction, formatting) scored by exact
match. The scorer is **conservative**: `"1,024"` for `1024` and a blank answer
both count as *not correct* — no partial credit, no similarity, no judge model.
`N=10` is stamped so this is read as a scoring *mechanism*, never a benchmark.
Feeding it from the team's real traffic needs an eval tap the gateway does not
emit yet — [`GAPS.md`](../../GAPS.md#quality-eval-tap).

## Measurement

- Cost: `mvp.learning.savings.summarize_savings` at built-in `rate_version`, over
  the checked-in `team_workload.jsonl` (bills recomputed by the real pricer, never
  hand-authored).
- Performance: **defined** as TTFT/TPOT; **not emitted** — the gap.
- Accuracy: exact-match over `mini_eval.jsonl`, conservative, `N` stamped.

## Expected result

The figures above are pinned to `run.py`'s output by
`backend/tests/test_scenarios_coverage.py`, so this document cannot silently drift
from what the code produces.

## Coverage & gaps

`coverage.yaml` encodes six steps: cost-savings and quality-exact-match are
`covered`; per-user cost split and the quality acceptance bar are
`user-responsibility`; TTFT/TPOT and the eval tap are `not-implemented` with
issue links. The cross-scenario roll-up is [`scenarios/COVERAGE.md`](../../COVERAGE.md).
