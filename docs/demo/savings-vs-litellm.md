<!-- Last updated: 2026-07-22 -->
<!-- Applies to: Stratoclave main. SYNTHETIC demo — not an audited tenant number. -->

# The number a spend log cannot produce: Stratoclave's Savings Certificate vs a passthrough gateway's spend log

A passthrough AI gateway (LiteLLM and the like) records, faithfully, **what was
billed**: per-model spend, token counts, request logs. That is a real and useful
thing. What it structurally *cannot* produce is the counterfactual that answers
the question a cost owner actually asks:

> "For the requests where the router advised a different model, if we had
> **followed** that advice, how much cheaper — or **dearer** — would this exact
> workload have been?"

Stratoclave's **Savings Certificate** answers it, priced apples-to-apples, with
the bias forced to the conservative side. This page shows the two side by side on
a tiny, checked-in, reproducible workload so you can see exactly where the line
is — and mutate the inputs to get the opposite result yourself.

> This is a **SYNTHETIC** demo produced by the real engine
> (`mvp.learning.savings`) over a seeded workload
> (`bench/savings/demo_workload.jsonl`). It is **not** an audited tenant number.
> The certificate stamps `traffic: synthetic` and the CLI prints a loud banner so
> it can never be mistaken for one. The headline figures below are deliberately
> **not** promoted into any README as a marketing claim (see "Honesty" below).

## Reproduce it (one command, no cloud, no network)

```bash
cd backend
python ../bench/savings/demo_offline.py            # human-readable certificate
python ../bench/savings/demo_offline.py --detail   # + per-request counterfactual rows
python ../bench/savings/demo_offline.py --json      # raw certificate JSON
```

The workload rows carry **no cost** — the demo recomputes each bill with the
shipped pricer (`_default_pricer`, built-in default rates), so the sample cannot
smuggle in a hand-picked flattering bill. Engine code is unchanged; the demo is
pure glue over the shipped `summarize_savings(rows, price=, resolve=)` seam.

## What the two systems see, on the same 9-request workload

The workload (all synthetic): 3 requests the router steered toward a cheaper model
(the tenant's bill ran dearer), **1 escalation** where the router advised a
*dearer* model, 2 the tenant already followed, 2 shadow-only advisories (logged,
not enacted), and 1 with no routing at all.

### A passthrough gateway's spend log can show

| Model | Requests | Billed spend |
|---|---|---|
| claude-opus-4-7 | 6 | $0.537500 |
| claude-haiku-4-5 | 2 | $0.025500 |
| claude-sonnet-4-5 | 1 | $0.036000 |
| **Total** | **9** | **$0.599000** |

That is the whole truth a spend log holds: money that actually moved. There is no
column for "what following the advice would have cost", because a passthrough
gateway never priced the counterfactual model over these same tokens.

### Stratoclave's Savings Certificate additionally shows

```
=== VSR Savings Certificate: tenant demo-offline day offline ===
  *** TRAFFIC: SYNTHETIC — SEEDED SAMPLE, NOT A REAL AUDITED TENANT NUMBER ***
  rate version:             builtin
  priced requests (base):   4
  billed over priced base:  $0.339000
  total billed (all reqs):  $0.599000
  NET saving:               $0.138000
    (+ cheaper-if-followed: $0.204000)
    (- dearer-if-followed:  $0.066000)
  net saving vs priced base: 40.7%
  request classes:          counterfactual=6, followed=2, no_suggestion=1
  quality measured:         False
```

Per-request (from `--detail`), every counterfactual is `recompute(billed_model)
− recompute(suggested_model)` at ONE rate snapshot over the SAME tokens:

| span | suggested → billed | recompute(billed) | recompute(suggested) | saving |
|---|---|---|---|---|
| req-save-0 | haiku → opus | $0.100000 | $0.020000 | **+$0.080000** |
| req-save-1 | haiku → opus | $0.087500 | $0.017500 | **+$0.070000** |
| req-save-2 | sonnet → opus | $0.135000 | $0.081000 | **+$0.054000** |
| req-loss-0 | opus → haiku | $0.016500 | $0.082500 | **−$0.066000** |

The escalation row (`req-loss-0`) is where the router advised the *dearer* model:
following it would have cost **more**, so it is **subtracted** from the net, not
hidden. That is the tell of an honest certificate — one that can show a loss is
one you can trust to show a gain.

## The four things the certificate does that a spend log structurally cannot

| | Passthrough spend log | Stratoclave Savings Certificate |
|---|---|---|
| **Counterfactual recompute** | No — only records the bill that happened | Yes — prices billed *and* advised model over the **same tokens** at **one** rate snapshot, so rate drift and cache asymmetry cancel |
| **Conservative bias** | N/A | Guaranteed VSR-*unfavourable*: escalation losses subtracted; `basis_drift` rows (a bill this recompute can't reconstruct) excluded, never inflated |
| **Escalation shown, not hidden** | N/A | `net = cheaper − dearer` can be **negative**; the dearer-if-followed magnitude is printed on its own line |
| **Audit reproducibility** | Log is a log | Rate-snapshot `version` stamped on the certificate; the same (tenant, day) recomputes to the identical number |
| **Realized vs potential** | N/A | Only *enacted* routing is in the headline; shadow-only advice is a **separate** upper-bound `potential` section, never summed in |

## On quality: what Stratoclave refuses to claim

The certificate prints `quality measured: False` — and leaves it there on purpose.
Stratoclave proves the **cost** counterfactual, but it does **not** claim the
routed traffic was as *good* as the tenant's original model, because it has not
measured that. A cheaper model that answers worse is not a saving; it is a
regression with a smaller invoice.

This is not a gap to paper over with a proxy metric — a proxy would be the exact
dashboard-estimate dishonesty the certificate exists to avoid. It is a deliberate
**refusal**: the quality line stays `false` until a **tenant's own eval** fills
it, and no saving should be externally claimed before then. A gateway that tells
you what it has *not* verified is demonstrating the same honesty that makes the
cost number worth trusting. (Wiring a tenant eval into the `quality` field is a
tenant-owned step; see `docs/design/vsr-savings-certificate.md`.)

## Honesty rules this demo follows

1. The headline figures are shown only next to the `SYNTHETIC` banner and are
   **not** transcribed into any README as a savings claim.
2. The workload includes a **non-zero escalation loss**, so the net is dragged
   down — no cherry-picked all-savings example.
3. `quality.measured` is displayed as `False`, unaltered.
4. The rate snapshot `version` and the checked-in workload are both inspectable,
   so a reader can change the inputs and produce the opposite result.

## Scope (what this is not)

This is a **courtesy sample**, not a benchmark of your workload. The number is
specific to this seeded traffic and the built-in default rates. Measuring your
own tenants' savings, defining the quality eval, and validating routing decisions
against your acceptance bar are **your** responsibilities — Stratoclave provides
the mechanism (the certificate engine, priced at ledger precision, reproducible
and conservative); the audited number is one you generate over your own traffic.
