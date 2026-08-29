<!-- Last updated: 2026-08-30 -->

# Contract: calibrated mode

The optional second half of the ceiling work. [`hard-ceiling.md`](hard-ceiling.md) is the first
half and includes the shadow measurement, the agreed refusal-rate target, and the
decision to switch gating on. All of that is a prerequisite here, and none of it lives
in this document — an earlier arrangement put the measurement here, which made the first
document unshippable on its own because nothing could establish that its bound was
operationally acceptable.

The verification for this stage lives in the same test files [`hard-ceiling.md`](hard-ceiling.md) names; the properties below are what an implementation has to satisfy.

## Status in the shipped code

**Not implemented.** Nothing in this document is switched on: the shipped bound is
the sound one from [`hard-ceiling.md`](hard-ceiling.md), with no calibration margin
and no calibration monitor. The document is the design for a later stage, kept here
so the trade it makes is on the record before anyone makes it.

## 1. What this buys, and what it gives up

The first contract's bound is sound, which costs in-flight admission headroom: a bound
several times the eventual charge means fewer concurrent requests for the same budget.
Calibrated mode trades soundness for tightness, for tenants who accept a weaker promise
in exchange for concurrency.

**It is not a ceiling guarantee and must not be described as one.** Use the first
contract's wording for what strict promises, including its exception clause; do not
restate it more strongly here.

## 2. The bound

A measured, monitored bound on tokens per byte per model, priced at the worst input-side
rate, in place of the byte count as a direct token bound.

The calibration comes from the shadow data the first contract produced: the realised
tokens-per-byte per model and per script, plus a margin you state. It is a `enforced`
setting in the first contract's taxonomy — it changes how the bound is derived, not
whether it gates.

## 3. What it admits, and what it still does not

It admits **S3-referenced images**, accepting the time-of-check-to-time-of-use race the
first contract refuses: the object can be replaced between sizing and sending. State
that in the mode's description.

It does **not** admit provider-side tool use, and there is nothing to admit.
`_reject_server_side_tools` in `backend/mvp/anthropic.py` refuses Anthropic's
server-executed tools on every route in every mode because Bedrock's Converse API cannot
express them, so no mode can offer them. An earlier version of this document listed them
as a calibrated feature, which was simply wrong about the code.

## 4. Monitoring the calibration, and failing closed

- Every settle reports the realised tokens-per-byte.
- A single settle above the calibration is a fail-closed event for that tenant: new
  admissions use the strict bound until an operator clears it. Not a warning.
- The switch follows the first contract's mid-period rule — it applies to admissions
  from that moment, and the strict guarantee is claimed only once every pre-switch hold
  has settled or been reaped. Do not halt admissions or drain.
- **Detecting a model or tokeniser change is part of this contract, not an assumption.**
  Name the signal, the source, what it is compared against, and what happens when the
  comparison fails. Without it the calibration silently expires and the first overrun is
  the notification. If you cannot build that detection, say so — it is a reason not to
  ship this mode at all.
- Refuse to ship if the shadow data contains any settle above the calibration.

## 5. Telling a shortage from a sizing problem

The first contract refuses a request whose bound exceeds the whole pool limit. Two
weaker signals belong here, because both need the shadow data to be set sensibly:

- **Will monopolise.** The bound exceeds `1/N` of `pool_limit` while staying below it.
  Do not refuse — it can legitimately succeed. Warn, naming the tenant and the ratio.
  `N` is the minimum in-flight concurrency the tenant is sized for, a tenant setting
  whose default comes from the shadow distribution rather than a guess.
- **Refused while mostly unused.** On every refusal **caused by the pool's conditional
  write failing**, record the reserved-versus-settled split, captured from that failed
  write rather than by a later read, or the numbers describe a different moment than the
  refusal. Refusals decided before the write — an unreadable image, a missing output
  ceiling — have no such split and are exempt. High reserved with low
  settled is the aggregate case — many mid-sized concurrent requests exhausting headroom
  while spend is low — and it is the difference between an operator resizing correctly
  and resizing blindly.

## 6. The operator-facing sentence

Mode is a per-tenant setting because the pool is per tenant. The sentence must carry the
same boundary the first contract requires — both named failure modes and the fact that
the guarantee rests on stated assumptions — rather than a shortened version of it. Take
the wording from the first contract's section 6 and add only the trade: calibrated is far
cheaper in concurrency and is not a guarantee.

## 7. Acceptance criteria

1. A settle above the calibration moves that tenant to the strict bound for new
   admissions without an operator acting first.
2. For the same Bedrock usage block, the settled charge and the recorded token fields do
   not depend on which bound was used. `reserved_microusd` does depend on it.
3. A model or tokeniser change is detected and reported before any settle exceeds the
   calibration, demonstrated by a deliberate change in a test environment.
4. Every refusal caused by the pool's conditional write failing carries the
   reserved-versus-settled split, captured at that write.
5. Calibrated admits S3-referenced images and its description says what that gives up.
