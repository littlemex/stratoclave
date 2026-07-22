<!-- Last updated: 2026-07-22. Numbers here are pinned to run.py output by
     backend/tests/test_scenarios_coverage.py — regenerate, don't hand-edit them. -->

# A small team on one shared budget pool

**Audience:** user (a team lead)
**Runs in two modes:** an **offline** mode (deterministic, CI-safe, no network) and
a **`--live` mode** that measures all three axes against **real Bedrock**.

## What you'll see

A three-person team (`acme-team`) sharing one budget pool. You walk the three axes
a team lead asks about — **cost, performance, accuracy** — first offline (the
mechanism, deterministic) and then live (the same mechanism, on real traffic):

- **Cost** — offline folds a checked-in workload into a Savings Certificate;
  **live** prices *real* Bedrock token usage with the shipped pricer.
- **Performance** — TTFT/TPOT are **measured client-side** against real Bedrock in
  `--live` (a live baseline). What is *not* yet emitted is a **gateway-side** TTFT
  telemetry to diff against — that is the real gap.
- **Accuracy** — the *same* conservative exact-match scorer runs offline on canned
  answers and **live** on the real model's output. Feeding it from a team's real
  production traffic needs an eval tap — a **gap**.

The gaps are the workshop's output — the next features to build, made
machine-visible with live evidence in [`COVERAGE.md`](../../COVERAGE.md).

## Prerequisites

- **Offline:** Python and this repo — no cloud, no network (a pure fold over
  checked-in data).
- **`--live`:** AWS credentials with Bedrock access (`AWS_PROFILE`,
  `AWS_REGION=us-east-1`). Costs real money — bounded by a hard `$0.10`/run cap in
  `live.py` (a full run is ~`$0.002`).
- **Responsibility boundary:** measurement, evaluation, availability targets, and
  backend fit are the operator's responsibility. This scenario provides the
  *mechanism* — a deterministic script, a metric definition, a shared scoring fold,
  and a live baseline harness — not an audited number for your workload. The live
  numbers are a **baseline with the gateway NOT in the path**, never a
  "gateway-verified" claim.

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

### 2. Performance — TTFT/TPOT offline is unmeasured; the live baseline measures it

Offline there is no model call, so `run.py` prints no TTFT and points at the
gateway-telemetry gap. The **`--live`** step (below) *does* measure TTFT/TPOT
client-side against real Bedrock. The genuine, remaining gap is a **gateway-side**
TTFT telemetry to diff the client number against — see
[`GAPS.md`](../../GAPS.md#perf-token-timing).

### 3. Accuracy — a tiny exact-match scorer (the same scorer runs offline and live)

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

### 4. `--live` — all three axes against real Bedrock (a live baseline)

```bash
AWS_PROFILE=... AWS_REGION=us-east-1 \
    python scenarios/usage/small-team/live.py --run-id demo1
```

This drives 10 tasks × 3 latency reps (30 real calls, ~`$0.002`) through
`claude-haiku-4-5` and measures every axis on real traffic. A committed sample run
([`results/live-demo1.json`](results/live-demo1.json)):

```
=== small-team LIVE BASELINE (real Bedrock, gateway NOT in path) ===
[COST]     total billed = $0.001686 (real token usage x shipped pricer)
[PERF]     TTFT p50=1074.5ms  min=873.9  max=1431.7  (N=30, raw kept)
[QUALITY]  10/10 exact-match (100%), conservative; norm-v1: strip + strip<=1 trailing punctuation + casefold
```

Read it honestly:

- **Cost** is priced from Bedrock's *own* token counts (not estimated) by the
  shipped pricer — the same engine as offline, now on real usage.
- **Perf**: TTFT is a **live baseline**, gateway *not* in the path. With N=30 the
  raw values are kept and only `p50` is named — no invented percentiles.
- **Quality**: the *same* conservative scorer as offline, on the real model's
  output. It scored `10/10` live versus `8/10` on the canned fixture — and *that
  gap is the lesson*: the real model returned `1024` without the comma the fixture
  assumed. The offline number tests the scorer; the live number tests reality.

The live results carry full provenance (`source=real`, model, region, timestamp,
N, run_id, `gateway_in_path=false`) so a live number can never be mistaken for an
audited or gateway-verified one.

## Measurement

- Cost: `mvp.learning.savings.summarize_savings` at built-in `rate_version` —
  offline over the checked-in `team_workload.jsonl`, live over Bedrock's own token
  usage. Bills recomputed by the real pricer, never hand-authored.
- Performance: TTFT/TPOT measured client-side over the streaming response in
  `--live`; a **gateway-side** telemetry to diff against is the gap.
- Accuracy: the shared conservative exact-match scorer over `mini_eval.jsonl`,
  `N` stamped — offline on canned answers, live on the real model's output.

## Expected result

The **offline** figures are pinned to `run.py`'s output by
`backend/tests/test_scenarios_coverage.py`, so the deterministic parts cannot
silently drift. The **live** figures quoted above are the frozen sample in
[`results/live-demo1.json`](results/live-demo1.json) (a CI test checks the doc
matches that committed file); a fresh `--live` run produces new, non-deterministic
numbers by design — CI never gates on them.

## Coverage & gaps

`coverage.yaml` encodes eight steps: cost-savings, cost-live-billing,
perf-ttft-client and quality-exact-match are `covered` (three carry live
evidence); per-user cost split and the quality acceptance bar are
`user-responsibility`; the gateway TTFT telemetry and the eval tap are
`not-implemented` with
issue links. The cross-scenario roll-up is [`scenarios/COVERAGE.md`](../../COVERAGE.md).
