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

`cost_if_suggested = actual_cost_microusd(suggested_pricing_key, input_tokens,
output_tokens)` — the suggested model priced over the request's REAL tokens.

Summary headline: **`net_saving = gross_saving − escalation_loss`**, where
`gross` sums only positive counterfactuals and `escalation_loss` sums the
magnitudes of the negatives. **`net` can be negative** — when the VSR routed a
workload cheap that then escalated dearer, or advised a dearer model than what was
billed, the certificate shows a LOSS. A certificate that can show a loss is one a
buyer trusts to show a gain. Escalation is never clipped to zero.

Coverage (`class_counts`, `priced_request_count`, `billed_microusd_over_base`) is
always explicit, so the figure is a partial sum with a stated base, never a
fabricated total.

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
- Tests: `test_savings.py` (unit + 300-example property: net = gross −
  escalation, no clipping, exhaustive classification), `test_savings_certificate.py`
  (end-to-end over moto: positive saving + surfaced escalation loss).

## Still to build (this is the wedge, not the finish line)

- The **quality signal** (tenant eval + judge) to make savings CLAIMABLE.
- The monthly certificate as a tenant-facing artifact (currently ops CLI).
- The decision-log **learning loop** (reconciled data → VSR training) — the
  compounding moat a follower cannot buy with time.
- Provider breadth via embedding the (MIT) LiteLLM adapter layer under the
  execution seam, so breadth stops being a differentiation axis at all.
