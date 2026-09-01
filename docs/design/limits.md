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

- a dollar pool for the current period marked `sizing = "per_seat"`, written at zero and
  maintained at `seats x $200` — zero is the honest ceiling for a tenant nobody is a member of
  yet, and the first membership brings it to one seat;
- a per-user token quota of ten million tokens, stamped onto the tenant as `default_credit` and
  resolved onto each membership as `total_credit`;
- no per-model quota.

The pool is the ceiling that binds first, and that is deliberate. It is set **below** the sum of any
per-user money ceilings a later change may add, because a per-seat pool equal to the sum of the
individual ceilings can never bind — the individuals would exhaust themselves first, and a ceiling
that cannot bind is decoration.

`seats` is the count of active memberships, and the pool equals it **at every moment** rather than
at creation only: a membership added or removed moves the limit and the headroom by exactly one seat,
as an atomic delta on the row, under the same guard the ceiling-set path uses. Nothing counts seats
by querying them, which is why the figure cannot drift from the memberships that produced it.

## 4. Where an operator's own figure takes over

The moment an operator sets a pool figure explicitly — through the admin route or, for their own
tenant, through the team-lead route — the row is marked `sizing = "fixed"` and stops following seats.
A figure a person chose is not overwritten by a later hire.

**A row with no `sizing` attribute is `fixed`.** Every pool row written before this document existed
has none, and each one is a figure someone set by hand; reading absence as `per_seat` would make
those ceilings start moving behind the operator's back.

## 5. What is bounded and what is not

The pool bounds **admission**: no request is admitted whose priced reservation does not fit. It is
not a bound on the invoice, because an outcome the gateway cannot observe is one it cannot price —
that boundary is stated in [CONTRACTS.md](CONTRACTS.md) under C1 and measured in
[MEASUREMENTS.md](../MEASUREMENTS.md).

The token quota bounds nothing in money, by construction of section 2. It is documented here so that
nobody reads its number as a budget, and so that an operator who wants a money ceiling per user knows
that today the answer is the per-model quota's user scope, one model at a time.
