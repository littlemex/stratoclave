<!-- Last updated: 2026-09-01 -->
<!-- Applies to: Stratoclave main -->

# Contract: what stops a request, and in what unit

A request is admitted only if every configured ceiling has room for it, and the ceilings are not
all denominated in the same thing. That difference is the source of every confusion this document
exists to end: **one unit can bound the bill and the other cannot.**

Code: [`backend/mvp/reserve_limits.py`](../../backend/mvp/reserve_limits.py) declares which ceilings
exist and, for each, the callable that turns its configured value into an item in the single
admission transaction. A ceiling that is configured and contributes no item is a bypass, so that
declaration is the list this document is written against.

## 1. The three ceilings

| Ceiling | Unit | Scope | On by default | What it protects |
|---|---|---|---|---|
| `tenant_dollar_pool` | integer micro-USD | one tenant, one calendar month | **Yes** — written at tenant creation and maintained at `seats x STRATOCLAVE_SEAT_MONTHLY_USD` (default `200`) | The bill. This is the ceiling that can be stated as a number an invoice can be compared against |
| `user_token_quota` | tokens | one user within one tenant | Yes, at a deliberately loose figure: `DEFAULT_TENANT_CREDIT`, default `10000000` | Fairness between the tenant's users — that one person does not consume the whole pool |
| `per_model_quota` | integer micro-USD | one tenant, or one user within it, per model, per month | No — opt-in per tenant | Spend on a specific model, e.g. "Opus, $50 a month" |

## 2. Why the units differ, and why only one of them is a ceiling

Tokens are what a caller can be held to **before** the call: `max_tokens` is a request parameter, so
a reservation can be taken against it without knowing any price. That is why the per-user quota came
first, and it is still the only quantity that can be reserved with no price lookup at all.

But a token count cannot say what it costs. Against the bundled floor's own measured prices
([`defaults/pricing.json`](../../backend/mvp/defaults/pricing.json)), one million tokens is **$0.25**
of Claude 3 Haiku input and **$27.50** of Claude Opus output — a 110-fold spread across models and
across legs of the same model. So a per-user figure of ten million tokens is somewhere between
$2.50 and $275 depending on what the user runs, which is not a budget.

**Tokens are therefore a per-request bound and a fairness device, never a money ceiling.** The
money ceiling is the dollar pool. Everything downstream agrees with that reading already: the ledger,
the rating path, `reprice`, the savings report and the per-model quota are all integer micro-USD, and
the per-model quota's write path refuses any other unit outright rather than reinterpreting a token
figure as dollars.

## 3. What a fresh deployment enforces

A tenant created through the ordinary route, with nothing set by hand, gets:

- a dollar pool for the current period that follows the seat count, written at zero and
  maintained at `seats x $200` — zero is the honest ceiling for a tenant nobody is a member of
  yet, and the first membership brings it to one seat;
- a per-user token quota of ten million tokens, stamped onto the tenant as `default_credit` and
  resolved onto each membership as `total_credit`;
- no per-model quota.

The pool is the ceiling that binds first, and that is deliberate. It is set **below** the sum of any
per-user money ceilings a later change may add, because a per-seat pool equal to the sum of the
individual ceilings can never bind — the individuals would exhaust themselves first, and a ceiling
that cannot bind is decoration.

`seats` is the count of active memberships, and the pool tracks it as the memberships change rather
than being sized once at creation: a membership added or removed moves the stored seat count, and on a
seat-tracked row moves the limit and the headroom by exactly one seat, as an atomic delta on the row.
The admission path never counts seats by querying them, so the ceiling costs no membership read.

Drift between the stored count and the memberships is possible, and it is checked rather than assumed
away. A delta applied twice moves the seat count and the ceiling together, in the same direction, so
every equation over the row still balances while the tenant admits an extra seat's worth of spend a
month. That is invisible to any intra-row check, so a daily reconciler counts the memberships and
compares. It reports and never repairs, because a repair would destroy the evidence of how the row got
that way and could only guess which side of the disagreement is right.

## 4. The rule: what the ceiling is, and how it is reversed

The ceiling is not a mode stored on the row. It is a rule over three attributes, and the mode falls
out of it:

```
seat_term  = seat_count x seat_rate
baseline   = manual_limit  if manual_limit is PRESENT  else seat_term
pool_limit = baseline + coalesce(pool_granted, 0)
```

`pool_granted_microusd` is any granted amount, and it is zero until grants exist. The identity is
written with the `coalesce` from the start so it is true of every row that exists today, rather than
becoming true when granting ships.

**Absence of `manual_limit_microusd` means "follow the seat count". Presence, INCLUDING zero, means
"this figure".** The sentinel has to be absence rather than zero, and the reason is not aesthetic:
`limit_usd_cents` accepts `0` today and it means every request refused, so reading `0` as "follow the
seats" would silently reverse the meaning of a request every existing caller can already make. No
existing caller can send absence, which is exactly what makes it a safe sentinel.

The moment an operator sets a figure explicitly — through the admin route or, for their own tenant,
through the team-lead route — the row holds that figure and stops following seats. A figure a person
chose is not overwritten by a later hire.

**The reversal is `{"follow_seats": true}`**, on the same endpoint, and it REMOVES the attribute
rather than writing the seat term into it. Writing the term back would leave a figure behind, and the
next hire would not move it. Before this existed, a figure set once stopped seat tracking permanently:
there was no request that could undo it, so a tenant that grew from four people to forty kept the
number somebody typed when it was four.

`seat_count` moves on every membership change whether or not money moves with it. On a row holding a
figure that means the row can still say its entitlement has outgrown the figure — which is the one
thing an operator cannot work out by looking at the figure.

**Every writer of this ceiling.** The list is derived from the row's own declaration
(`dynamo.tenant_budgets.ceiling_writers()`, from `POOL_ROW_ATTRIBUTES`) rather than restated here,
because a list written out in prose passes review while naming a subset the moment a writer is added:

- `TenantBudgetsRepository.set_manual_limit` — an operator's figure (admin or team-lead route).
- `TenantBudgetsRepository.clear_manual_limit` — the reversal.
- `TenantBudgetsRepository.adjust_pool_for_seat_delta` — a membership change; the ONE seat-delta
  writer, which every membership transition including a user deletion routes through.
- `TenantBudgetsRepository._seed_pool_row` — creation, and the period rollover's new row.
- `migrations.pool_ceiling_migration` — the backfill and the seat-rate recompute.

**The rate is not a live knob.** Each row stores the per-seat rate its own ceiling was computed at, so
that ceiling is reproducible; the deployment records the rate in force once. A process configured with
a different figure **refuses to start**, because a ceiling recomputed at a rate nobody chose is a
perfectly plausible number and nothing afterwards can tell it from a correct one. Changing the rate is
`migrations.pool_ceiling_migration --recompute-seat-rate`, which recomputes every seat-tracked row and
leaves rows holding an operator's figure alone.

**The period boundary has an owner.** A new calendar month's row is created from the previous month's,
carrying the attributes the declaration classifies as carried and recomputing the rest, so a
seat-tracked row arrives seat-tracked with the same seats and a row holding a figure arrives holding
it. What does not arrive is the spend, the reservations, or the granted term.

**The migration to this rule is one-shot.** No phase of it may be re-run once grants exist. Its
cut-over reads a row carrying neither new attribute as `manual_limit = pool_limit`, which is right
while the total is only ever a baseline and destructive once the total can also contain granted money:
the grant would be folded permanently into the operator's figure, on every such row at once. The rule
that makes the cut-over safe beforehand is exactly what makes a re-run unsafe afterwards, so every
phase refuses outright on a table where any row carries a granted amount.

## 5. What is bounded and what is not

The pool bounds **admission**: no request is admitted whose priced reservation does not fit. It is
not a bound on the invoice, because an outcome the gateway cannot observe is one it cannot price —
that boundary is stated in [CONTRACTS.md](CONTRACTS.md) under C1 and measured in
[MEASUREMENTS.md](../MEASUREMENTS.md).

The token quota bounds nothing in money, by construction of section 2. It is documented here so that
nobody reads its number as a budget, and so that an operator who wants a money ceiling per user knows
that today the answer is the per-model quota's user scope, one model at a time.
