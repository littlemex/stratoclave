<!-- Japanese translation: MEASUREMENTS.ja.md -->
<!-- Last updated: 2026-08-30. Every figure carries the conditions it was measured under. -->

# What a failed LLM call actually costs

Amazon Bedrock does not document which failures are billed. So it was measured,
against the provider's own counters, and the answers contradict what almost every
client library assumes. This page is the result. It is useful whether or not you
ever run Stratoclave: the facts are about Bedrock and the AWS SDK, not about this
project.

Each finding says where it stops, and three of the five are reproduced by the
harness at the end; the other two say what part of them is not.

Read this first if you are building anything that meters, budgets, or resells LLM
calls. The short version is that **"my client got nothing" is not evidence that
nothing was billed**, and a budget built on that assumption leaks money it can
never see.

## The five findings

| A common assumption | What was measured | Where it stops |
|---|---|---|
| An error means no charge | A Converse call **abandoned at a 2 s client read timeout** was billed **1,493 output tokens**. The caller received nothing | One trial, one model family. It shows that this SDK-side read timeout did not stop execution — not that every timeout behaves so |
| A charge follows the HTTP status | The line is **whether the model ran**, not whether the call failed. A service rejection (`ValidationException`, HTTP 400) records an invocation but **no token counters**; a completed stream records exact usage | Only `ValidationException` was measured. Other rejection codes and HTTP statuses inherit that zero **by argument, not by counter** |
| `Invocations` counts what you pay for | `Invocations` **also counts rejections**, which produced no token counters. The token counters are the signal closest to the bill | It is a count, so it cannot distinguish billed from unbilled or say how much either way |
| `max_attempts: 1` means one call | botocore resolves it to `total_max_attempts = 2`, and rewrites the caller's own dict in place | Reproduced offline against botocore 1.35.99. The resulting **second charge** was measured in the same account, but the harness here checks the resolution, not the charge |
| An abandoned call is unattributable | With model invocation logging on, the abandoned call's record was retrieved **by a `requestMetadata` marker alone**, carrying `in 22 / out 1,493` | Two records observed, delivered 35 s and 41 s after the call. **Converse only**: on the OpenAI-compatible endpoint the marker is recorded as `null`. Retrieval is by hand — the harness stamps the marker, it does not search the log |

Everything above was measured on **2026-08-29, us-east-1**, one condition per
minute, on models the account otherwise never invokes (so the counters could not
be contaminated by other traffic), reading CloudWatch's own `AWS/Bedrock` metrics
rather than any client-side estimate. Those metrics are the provider's telemetry
rather than an invoice line: on-demand tiers price per token, so "was billed 1,493
output tokens" means the provider recorded 1,493 output tokens for a call whose
caller received nothing.

These are existence proofs, not distributions. One counter-example is enough to
overturn "an error is free", which is why a single trial settles that — and it is
not enough to state a rate, which is why no rate is stated anywhere here.

**Terms used above**, once each: *Converse* is Bedrock's own request API (as
opposed to its OpenAI-compatible endpoint); a *read timeout* is your client giving
up on a response it is still waiting for; *model invocation logging* is a Bedrock
setting that writes one record per call to S3 or CloudWatch Logs, optionally
without the prompt and response text; *`bedrock-mantle`* is the older
OpenAI-compatible surface, which Stratoclave no longer uses; *CUR* is the AWS Cost
and Usage Report, the itemised billing export.

## Finding 1: your timeout is not the model's timeout

A client read timeout is a decision your process makes about how long it is
willing to wait. In this measurement nothing about it reached the provider: the
model kept generating, finished, and the tokens it produced were recorded against
the account.

Measured: one Converse call, one SDK attempt, a 2 s read timeout, an input the
model would answer at length.

| Signal | Value |
|---|---|
| What the caller received | nothing (a `ReadTimeoutError`) |
| `Invocations` | 1 |
| `OutputTokenCount` | **1,493** |
| Invocation log record | `in 22 / out 1,493` |

The consequence for anything that holds a budget: on this failure the reservation
must **not** be returned. Returning it hands back money the account really spent,
and lets the next admission spend it a second time. In Stratoclave that decision
lives in one table (`backend/mvp/provider_outcome.py`) whose rows cite this
measurement, and in one object that every route must use to end a reservation
(`backend/mvp/_money.py`).

## Finding 2: the line is whether the model ran

Failures are not one category. Five conditions were run, and they fall into three
classes that cost three different amounts:

| Condition | `Invocations` | Token counters | Billed? |
|---|---|---|---|
| Our own serialiser refused the request (botocore `ParamValidationError`) | none | none | no — the request never left |
| The service rejected it (`ValidationException`, HTTP 400) | 1, plus 1 `InvocationClientErrors` | **none** | no — a rejection happens before a model runs |
| The stream was closed by the consumer after two events | — | **none recorded** | no |
| The call was abandoned on a read timeout | 1 | **1,493 output** | **yes** |
| The stream completed | 1 | exact usage | yes |

So the useful question is not "did it throw?" but **"how far did it get?"**: never
sent, rejected before inference, or sent with no terminal evidence. Only the third
one is expensive, and it is also the one that looks like a network error.

Anything unrecognised belongs in that third category. Being wrong in that
direction costs a tenant some headroom for a while; being wrong in the other
direction breaks the budget limit. Those are not symmetric, so the default is not
symmetric either.

## Finding 3: `Invocations` is not a billing proxy

This one is a trap in the other direction. `Invocations` counted the rejection
that was **not** billed, and the same counter is what most cost dashboards reach
for first. Read it, and a rejection storm looks like spend. Read the token
counters, and it looks like what it is.

Pricing on the on-demand tiers is per token, so the token counters are the closest
thing to the bill that is visible in near-real time. Neither classic CUR nor CUR
2.0 carries a per-request identifier, so invoice-level per-request reconciliation
is not available to anyone — not to you, and not to any gateway.

## Finding 4: one reservation, several provider invocations

`botocore.config.Config(retries={"max_attempts": N})` does not mean *N attempts
total*. botocore rewrites it to `total_max_attempts = N + 1`. Measured:

| Config | Resolved by botocore | Counted invocations |
|---|---|---|
| `retries={"max_attempts": 1}` | `total_max_attempts: 2` | **2** |
| `retries={"total_max_attempts": 1}` | `total_max_attempts: 1` | 1 |

It also rewrites the dict you handed it, **in place**. On botocore 1.35.99 the
literal `{"max_attempts": 1}` you passed reads back as
`{"total_max_attempts": 2, "mode": "legacy"}` after the client is built, so a
caller cannot inspect its own request afterwards to find out what it asked for.

If a retried attempt succeeds, the earlier attempt's tokens are still billed, and
the request your code sees is a **success**. No error handling anywhere in your
stack observes that charge. If you meter per request, you have just under-counted
by however many attempts the SDK made silently.

What to do: set `total_max_attempts` explicitly, and keep the retry budget
somewhere you can account for — a router loop you wrote, not a config key the SDK
reinterprets.

## Finding 5: an abandoned charge is attributable, if you prepare for it

A caller that abandons a call never learns the provider's request id. And in an
invocation log record, `identity` is the gateway's own task role — identical for
every tenant. So by default there is nothing to join a mystery charge to.

One thing survives: Bedrock echoes `requestMetadata` into model invocation log
records. Measured — the record for the call abandoned on a read timeout was
retrieved **by that marker alone** and carried the exact token counts.

| Property | Measured |
|---|---|
| Retrieval key | `requestMetadata` marker, no request id needed |
| Record contents | `in 22 / out 1,493` |
| Delivery lag to the log destination | 35–41 s |
| Text delivery | off — the record is metadata only |

Limits, stated plainly: this is **Converse only**. On the OpenAI-compatible
endpoint the log record exists but `requestMetadata` is recorded as `null`, so
those attempts are aggregate evidence rather than per-request attribution. And
`bedrock-mantle` produces no record at all — which is one of the reasons
Stratoclave moved its OpenAI-compatible traffic to the endpoint AWS recommends.

Because the marker has to be on the request *before* it is sent, stamping it is a
correctness requirement rather than an optimisation. Values are ids the gateway
minted: no prompt, no user identifier, nothing derived from request content.

## Two measurements about test environments, while we are here

Neither is about Bedrock, and both cost people days.

| Finding | Measured |
|---|---|
| A stand-in store makes local timing **meaningless**, not merely inflated | the same call: `reserve_ms=13.7` on DynamoDB Local (Amazon's local build of the real engine) vs `reserve_ms=9485.2` on moto (an in-process Python mock) — reserve alone ~**690×** |
| The ledger is not the cost of a gateway request | `reserve_ms=13.7` + `settle_ms=13.3` against `upstream_ms=1217.2` — 27 ms of bookkeeping in a 1.26 s request |

If you are benchmarking a gateway against a mocked datastore, you are
benchmarking the mock.

## Reproducing this

`scripts/local/measure_provider_outcome.py` encodes the five conditions and reads
the provider's own counters back, which covers findings 1 to 3. It also resolves the
retry configuration of finding 4 offline. What it does not do is search the
invocation log for finding 5 — it stamps the marker, and retrieving the record is a
manual step against your own log destination. It needs credentials that can invoke Bedrock and
read CloudWatch, it spends real money (a few cents), and it deliberately runs
**one condition per minute** so that each condition owns its counter minute:

```bash
python3 scripts/local/measure_provider_outcome.py --region us-east-1 \
  --model us.anthropic.claude-haiku-4-5-20251001-v1:0 \
  --out /tmp/provider-outcome.json
```

Use a model the account otherwise does not invoke, or the counters will include
other people's traffic. The script prints the table above with your own numbers
and writes the raw counter reads next to them, and it refuses to summarise a
condition whose counter minute came back empty rather than reporting a zero it did
not measure.

The retry finding needs no account at all:

```bash
python3 scripts/local/measure_provider_outcome.py --retry-arithmetic-only
```

## Where these facts are load-bearing in this repo

- `backend/mvp/provider_outcome.py` — the states (observations) and the liability
  policy (decisions), one versioned row per finding, each row citing its
  measurement and naming its accepted risk.
- `backend/mvp/_money.py` — the hold: the only object that may end a reservation,
  so every route asks the policy above and no route can answer for itself.
- `docs/design/charge-loss.md` — the contract these measurements serve: attempts,
  evidence, liability, and what cannot be done from inside a gateway.
- `docs/EVIDENCE.md` — the full evidence map, including what is *not* verified.
- `backend/tests/test_provider_outcome_formal.py`,
  `backend/tests/test_money_lifecycle_discipline.py` — the properties and the
  static guard that keep a future route from quietly refunding a billed call.
