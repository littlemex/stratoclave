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
- **Live (gateway path, §4):** AWS credentials with Bedrock access
  (`AWS_LIVE_PROFILE`, `AWS_REGION=us-east-1`). Costs real money — bounded by a hard
  `$1`/run cap in `live_gateway.py` (a full run is ~`$0.001`).
- **Live (direct baseline, §5):** same credentials; `$0.10`/run cap in
  `baseline_direct.py`.
- **Responsibility boundary:** measurement, evaluation, availability targets, and
  backend fit are the operator's responsibility. This scenario provides the
  *mechanism* — a deterministic offline script, real-Bedrock harnesses, a metric
  definition, and a shared scoring fold — not an audited number for your workload.
  The gateway-path numbers verify the gateway's **logic and billing on real model
  traffic** via in-process ASGI; they are **not** a deployed-environment SLO
  (no network/ALB in the path — see the `excluded` labels).

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

### 4. Gateway path — all three axes on data that crossed the gateway (the real one)

This is the verification the workshop exists for: the tasks go **through the
gateway's own request path** (`POST /v1/messages`: auth → reserve → real Bedrock →
settle → ledger), so cost is the **charge-of-record the gateway settled**, TTFT is
the **gateway path** latency, and quality scores the **gateway's** response.

```bash
SC_GW_LIVE=1 AWS_REGION=us-east-1 [AWS_LIVE_PROFILE=claude-code] \
    python -m pytest backend/tests/test_live_gateway.py -s -q
```

A committed sample run ([`results/live-gateway-gw1.json`](results/live-gateway-gw1.json)):

```
=== small-team GATEWAY-PATH live (real Bedrock, gateway IN path) ===
    transport=in-process-asgi  ledger=moto (not real DynamoDB)
[COST]     charge-of-record (ledger settle) = $0.000492  (client-side estimate $0.000562)
[PERF]     gateway TTFT p50=2384.4ms  direct TTFT p50=2089.4ms
           paired overhead median=248.7ms (min=-320.2 max=778.6, N=10, point est.)
[QUALITY]  10/10 exact-match (100%), conservative, on the gateway response
```

Read it honestly:

- **Cost** is the **charge-of-record the gateway wrote to the ledger** ($0.000492),
  read back after settle — not a client guess. It sits just under the client-side
  estimate ($0.000562), so the gateway's billing tracks real usage.
- **Perf** is a **paired** measurement in the SAME run: the gateway path vs the
  direct path per task (order alternated). The **gateway overhead** — the number a
  direct call can never give you — is the paired median **248.7ms** (auth + reserve +
  ledger + ASGI dispatch). N=10 → a **point estimate only**, no distribution claim;
  the negative min shows real jitter, kept honest rather than hidden.
- **Quality** scores the **gateway's** response `10/10` with the same conservative
  scorer.

Honest labels, stamped in the result: `gateway_in_path=true`, but
`transport=in-process-asgi` and `ledger=moto` with an `excluded` list
(`network, ALB, TLS, process-boundary`). This verifies the **gateway's logic and
billing accuracy on real model traffic** — it is **not** a deployed-environment SLO
(no network/ALB in the path).

### 5. Direct baseline — the measuring stick (verifies nothing on its own)

```bash
AWS_REGION=us-east-1 python scenarios/usage/small-team/baseline_direct.py --run-id demo1
```

Drives the same 10 tasks × 3 reps **directly** to Bedrock (gateway NOT in path) —
sample [`results/live-demo1.json`](results/live-demo1.json): cost `$0.001686`, TTFT
p50 `1074.5ms` (N=30, raw kept, min `873.9` / max `1431.7`), quality `10/10`. It
exists only as the baseline the gateway path (§4) is diffed against; its
`path=direct-bedrock-no-gateway` label and a stderr warning say so. Its `10/10` vs
the offline `8/10` is the lesson: the real model returned `1024` without the comma
the canned fixture assumed.

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
