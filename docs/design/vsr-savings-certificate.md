<!-- Last updated: 2026-07-20 -->
<!-- Applies to: Stratoclave main -->

# Design: the VSR Savings Certificate (the core weapon vs LiteLLM)

Stratoclave competes with LiteLLM **head-on** as a general AI Gateway. The way it
wins is not "we also have semantic routing" — LiteLLM can ship a routing plugin in
a quarter. It wins by closing a loop LiteLLM **structurally cannot** close:

> routing judgment (VSR) → execution → **ledger-precision charge** → span_id
> reconciliation → a **counterfactual savings figure a CFO can audit**.

Every "we cut LLM cost 70%" claim in the market shares two holes: the saving is a
dashboard *estimate* (not reconciled to the invoice), and the quality of the
cheaper route is *asserted, not measured*. This design produces a number without
the first hole and refuses to hide the second.

## What the certificate answers

For a `(tenant, period)`: **"if the tenant had followed the VSR's routing advice,
how much cheaper (or dearer) would this exact traffic have been?"** — priced from
the SAME versioned rate table the ledger charges from, over the SAME token counts
each request actually produced (read off the billed usage row, never
re-estimated).

## Why LiteLLM cannot produce this (structural, not effort)

1. **No ledger-precision baseline.** LiteLLM's per-team budget is a cached
   spend-counter check-then-act (a TOCTOU soft limit). Its cost tracking is
   observability, not a charge of record. Stratoclave's `billed_microusd` is a
   settled ledger charge (Z3-proven no-oversell). A counterfactual is only as
   trustworthy as its baseline.
2. **No decision↔charge join contract.** Computing "if followed the VSR" requires,
   at decision time, a schema'd record of *what the VSR advised* keyed to the
   request, joinable to the *billed* usage. LiteLLM's callback logs are arbitrary
   notifications; retrofitting a joinable contract is a rewrite of every
   integration. Stratoclave writes `decision#…` / `outcome#…` records joined by
   `(run_id, span_id)` from day one.
3. **No trust boundary.** The VSR's suggestion passes the same allowlist as a
   client pin and never touches money, so an aggressive routing model can never
   oversell. LiteLLM's generic hooks have no such judgment/execution/money
   separation to point to.

## The computation (honest accounting is the product)

Pure fold in `backend/mvp/learning/savings.py` over the reconcile-join rows
(`vsr_reconcile.reconcile_join`, which now carries the billed `input_tokens` /
`output_tokens`). Per VSR-acted request, classified into exactly one bucket so
none is silently dropped:

| class | meaning | contributes |
|---|---|---|
| `no_suggestion` | VSR steered nothing (passthrough/timeout) | 0, out of base |
| `unmatched` | no billed usage row (coverage gap) | 0 |
| `no_tokens` | matched but no token counts (data gap) | 0 |
| `unpriceable` | suggested model has no pricing key (data gap) | 0 |
| `followed` | billed model already == suggested (saving already in the bill) | 0 (never double-counted) |
| `counterfactual` | billed ≠ suggested → `saving = billed − cost_if_suggested` | signed |

**MODEL-VS-MODEL AT ONE SNAPSHOT (Fable review, findings 1 + 3).** The billed
usage row records only `(model_id, input_tokens, output_tokens, cost_microusd)` —
NO pricing version and NO cache-token breakdown. So `billed_microusd −
actual_cost(suggested)` would mix a past, versioned, cache-inclusive charge with a
present, cache-free estimate — a double asymmetry that BOTH fall VSR-favourable
(saving inflated). Instead we price BOTH models at ONE rate snapshot over the SAME
tokens:

```
saving = recompute(billed_model, in, out) − recompute(suggested_model, in, out)
```

Both legs share the identical rate basis and identical (cache-free) token
treatment, so rate drift and cache asymmetry **cancel exactly** — the saving
depends only on the rate DIFFERENCE between the two models (proven in
`test_savings_z3`). We also recompute the billed model and compare to the actual
`cost_microusd`; a divergence beyond tolerance is classified `basis_drift` and
EXCLUDED (a stale/cache-heavy basis never silently inflates savings). The rate
version is stamped on the certificate so a past `(tenant, day)` recomputes to the
same number (audit reproducibility). `followed` = SAME `bedrock_model_id` (not
merely same pricing key), and a matched row with no `cost_microusd` is `no_cost`
(never a fake `−cf` loss).

Summary headline: **`net_saving_microusd`** at top level; the decomposition
(`positive_deltas_microusd` / `negative_deltas_microusd`) is nested under
`decomposition` with deliberately un-promotable names so a report cannot
cherry-pick a gross figure (Fable finding 4). **`net` can be negative** — a
workload the VSR routed dearer than what was billed shows a LOSS. A certificate
that can show a loss is one a buyer trusts to show a gain.

Coverage is explicit AND spend-weighted: `class_counts` (per class), plus
`class_billed_microusd` and `total_billed_microusd_all_classes` so a saving % is
never computed against a cherry-picked subset (Fable finding 5) — the honest
denominator (total billed across all classes) is always in the output.

## Quality is NOT asserted here

The money side is exact; **routing quality (did the cheaper model actually solve
the task?) is a separate signal** — the VSR's own metric plus a tenant-defined
eval. The certificate carries `quality: {measured: false}` until that fills it,
and **no saving is externally CLAIMED before quality parity is confirmed**. The
honest metric is "quality-adjusted cost per solved task", assembled once the eval
signal lands (roadmap).

## Rollout: Shadow → Canary → Full (the adoption wedge vs LiteLLM)

1. **Shadow.** The VSR is consulted and logged, but execution stays on the client
   pin. The certificate then reports "if you had followed the VSR" over the
   tenant's real traffic — **zero tenant risk, a savings number before any
   behaviour change**. This is the wedge: "put Stratoclave behind your existing
   LiteLLM deployment in passthrough+shadow for two weeks and get an audited
   savings report on your own traffic."

   **Implemented (litellm-wedge slice-2):** a minimal LOCAL rule judge
   (`mvp.vsr.shadow`) is wired into all three model routes. When no real VSR or
   pin decides routing, it attaches a `shadow-advised` decision to the
   reserve-time decision record — advisory-only (never a routing pin, no response
   header, no money effect). It is DARK BY DEFAULT (`STRATOCLAVE_SHADOW_VSR`):
   off, the judge never runs and extracts no request features. The advice lands
   in the certificate's `potential` (enacted=False) base only, never the realized
   headline, with an explicit upper-bound caveat (quality unmeasurable — the
   suggested model never ran). A stronger judge is a drop-in replacement for the
   rule engine; the accounting boundary does not move.
2. **Canary.** N% of traffic executes the VSR's choice; quality judged by the
   tenant eval + LLM-as-judge (dual — the judge alone is not trusted).
3. **Full + monthly certificate.** With continuous audit.

**Do not publish comparative anti-LiteLLM marketing until Shadow numbers exist.**
A product whose weapon is *proof* must never ship an unproven claim — that burns
the one asset (credibility) the whole strategy rests on.

## Surface

- `backend/mvp/learning/savings.py` — `counterfactual_row`, `summarize_savings`,
  `savings_certificate(tenant_id, day)`. Pure fold + thin reconcile reader.
- `backend/mvp/learning/savings_cli.py` — `python -m mvp.learning.savings_cli
  --tenant <id> --day YYYYMMDD [--json] [--detail]`. Internal ops path, no
  request-path code, no new table.
- `backend/mvp/vsr/shadow.py` — the local rule judge (`propose`) + request-path
  seam (`shadow_vsr_decision`) + schema feature extractors. Dark by default,
  advisory-only. Wired at the three model routes (`mvp/anthropic.py`,
  `mvp/chat_completions.py`, `mvp/openai_responses.py`) just before the reserve
  chokepoint; the decision rides `reserve_credit_for_model(vsr_decision=...)`.
- Tests: `test_shadow_vsr.py` (rule judge + seam: dark/fail-open/classification/
  mode-independence), `test_shadow_vsr_wiring.py` (per-route flag on/off: served
  model unchanged, pin/tools suppression, propose-never-called when dark).
- Tests: `test_savings.py` (unit + 300-example property: net = gross −
  escalation, no clipping, exhaustive classification), `test_savings_certificate.py`
  (end-to-end over moto: positive saving + surfaced escalation loss).

## Auto-issue (litellm-wedge slice-4)

The certificate computation is a fold; slice-4 makes it a **durable, auto-issued
artifact** so a tenant gets an audited record without an operator running the CLI.

- `backend/mvp/learning/certificate_store.py` — `issue_certificate` (pure: decide
  if the day is honestly certifiable), `store_certificate` (WRITE-ONCE via
  `attribute_not_exists`; a re-run is a no-op, a genuine recompute is a NEW
  `revision` that `supersedes` the old, never an overwrite), `issue_and_store`,
  and `issue_for_tenants` (the scheduler body — per-tenant try/except isolation).
  Stored on the routing-signals table under a `CERT#<tenant>` / `cert#D#<day>#r#<rev>`
  key namespace (no new table).
- `backend/mvp/learning/certificate_cli.py` — `issue` / `get` ops face.

Honesty guards are runtime invariants, not tests (Fable slice-4 design):
  - **data-absent != $0.** A day with no VSR-acted traffic is a documented SKIP
    (`SKIP_NO_TRAFFIC`), never a $0 certificate (which would lie "we saved
    nothing" about a day we could not measure).
  - **coverage gate.** A day whose reconcile is >10% unsettled is skipped
    (`SKIP_UNMATCHED_HIGH`) rather than stamped `final` at an understated number.
  - **no synthetic in the store.** `store_certificate` refuses any provenance
    other than `real`.
  - **caveats are load-bearing.** It refuses a certificate that dropped its
    honesty caveats (quality unmeasured, potential is an upper-bound estimate).
  - **injected clock.** No module here reads a clock; `generated_at_ms` is passed
    by the caller (the Lambda handler, from the EventBridge event `time`).

Deploy leg (CDK, wired separately): a daily EventBridge rule → Lambda that calls
`issue_for_tenants` for the previous settled day (**D+N**, N = a settle window;
the backend coverage gate then refuses to finalize any day that is STILL under-
settled after N — the two are complementary: D+N gives settle time, the gate
refuses to stamp `final` on a day that never settled). Shadow-stage certificates
are an INTERNAL artifact; the tenant-facing HTTP surface is a later slice (per the
rollout rule — no external claim before Shadow numbers exist).

Alarms the Lambda must emit (honesty depends on them, not just on the backend):

- **per-run failure**: `BatchIssueReport.failed` non-empty → a tenant errored.
- **silent-skip / issued < expected**: issued count < active-tenant count.
- **fleet-wide NO_TRAFFIC (outage vs quiet)**: SKIP_NO_TRAFFIC cannot itself tell
  "the VSR genuinely acted on nothing" from "the decision-log ingestion was down"
  (backend note on SKIP_NO_TRAFFIC). A SINGLE tenant's quiet day is normal; ALL /
  most tenants skipping NO_TRAFFIC on the same day is an ingestion outage — alarm
  on that separately so honest-absence never masks an outage.
- **consecutive-skip series**: a tenant skipping many days in a row (e.g. a
  mis-config stuck at UNMATCHED_HIGH) is healthy per-day but anomalous as a
  series. The daily batch report looks fine, so this must be tracked across runs.
  To count it, the scheduler must PERSIST skips (a `skip` row per tenant/day, or a
  CloudWatch metric per skip_reason) — a skipped day should be auditable too, so
  a gap in the certificate series is explained, never a silent hole. **Follow-up:
  persist skip rows; until then the Lambda emits a per-skip_reason metric.**

Backend follow-ups (recorded, not silent): tune `DEFAULT_MAX_UNMATCHED_FRACTION`
(0.10) from the observed unmatched distribution once the scheduler has run;
persist skip rows for series-level auditing.

Slice-4-CDK follow-ups (Fable close, handoff — not blockers):
- **First-deploy smoke**: verify (a) the real EMF log line matches each metric
  filter's `$.field` pattern (a mismatch silently zeroes the issued/failed/
  no-traffic metrics — only `unaccounted` fails loud via MISSING=BREACHING), and
  (b) the shared monitoring picks up the three new alarms (if it selects by name
  prefix / explicit list rather than auto-collecting all).
- **`expected` self-report blind spot**: `unaccounted` is the handler's own
  count, so a bug in enumerating `expected` (pagination gap, filter error) passes
  as `unaccounted==0`. Emit `expected` as its own metric and monitor a sudden
  drop / 0, or add a periodic tenant-count reconcile.
- Wire a DLQ + `retryAttempts` on the schedule target; note the fleet-NO_TRAFFIC
  fraction is statistically meaningless at 2–3 tenants (add an absolute-count
  compensating check while the fleet is small); persist skip rows to make
  consecutive-skip-per-tenant alarmable.

## Still to build (this is the wedge, not the finish line)

- The **quality signal** (tenant eval + judge) to make savings CLAIMABLE.
- The monthly certificate as a tenant-facing artifact (currently ops CLI).
- The decision-log **learning loop** (reconciled data → VSR training) — the
  compounding moat a follower cannot buy with time.
- Provider breadth via embedding the (MIT) LiteLLM adapter layer under the
  execution seam, so breadth stops being a differentiation axis at all.
