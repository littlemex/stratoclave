"""Shared credit-reservation pipeline for the Anthropic Messages route and
the OpenAI Responses route.

Both routes share the exact same money-flow:

    1. authenticate the caller
    2. resolve and allowlist the model
    3. estimate a reservation (input + max_output) and atomically debit it
    4. invoke Bedrock
    5. settle the reservation against the actual usage and append a
       UsageLogs row

Steps 3 and 5 are protocol-agnostic and live here so the two route handlers
cannot drift in their credit semantics.

Two budget layers, one atomic reservation
------------------------------------------
Every request always debits a **per-user token balance** (`UserTenants`). When
the caller's tenant additionally has a **dollar pool budget** for the current
period (`TenantBudgets`), the same request also reserves the request's cost in
micro-USD from that shared pool. Both debits happen inside a single DynamoDB
`TransactWriteItems`, so neither the per-user cap nor the tenant pool can be
raced past, and a request that would breach *either* is rejected wholesale
with HTTP 402. The 402 `reason` distinguishes `personal_budget_exhausted` from
`tenant_pool_exhausted` so operators and clients can tell which ceiling hit.

A tenant with no pool row for the period keeps the original single-table,
per-user-token behaviour untouched (pool budgeting is opt-in per tenant).

Each route owns its own minimum-reservation floor (`anthropic.py` uses 1024;
`openai_responses.py` uses 8192).
"""
from __future__ import annotations

import os
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

import boto3
from botocore.exceptions import ClientError
from fastapi import HTTPException

if TYPE_CHECKING:
    from .pricing import RateSnapshot

from core.logging import get_logger
from dynamo import UsageLogsRepository, UserTenantsRepository
from dynamo.tenant_budgets import (
    TenantBudgetsRepository,
    current_period,
)
from dynamo.tenant_budgets import hold_sk as _hold_sk
from dynamo.tenant_budgets import previous_period as _previous_period
from dynamo.user_tenants import CreditExhaustedError


logger = get_logger(__name__)

# Reserving the pool touches a single hot item (the tenant's period row), so
# under contention the optimistic snapshot lock loses races. We retry more
# generously than the per-user path and back off a touch between attempts so a
# thundering herd does not exhaust the budget in microseconds. Crucially, when
# the retries ARE exhausted we fail *closed* (see reserve_credit): a pooled
# tenant must never have a request slip through unpriced just because the pool
# row was hot.
# The reserve is a single hot-row optimistic write: under an N-way concurrent
# burst on one tenant, at most one writer wins per round, so the last writer
# needs ~N rounds to drain. 12 rounds + full-jitter backoff comfortably clears
# a 20-way burst; the reserve completes *before* the Bedrock call, so these
# retries add latency only during genuine contention, never to a quiet request.
# docs/design/hard-ceiling.md section 6: a sound bound can be several times the
# eventual charge (how much depends on the script — deliberately not quoted as
# a ratio here, see reservation_bound.py's module docstring), so a single
# request's bound can occupy a material fraction of a tenant's pool_limit as
# HEADROOM alone, long before real spend is anywhere near the ceiling.
#
# Three DISTINCT conditions follow, reported differently (an earlier draft
# collapsed the first two into one fraction-based REFUSAL, which review found
# wrong in both directions):
#
#   - "does not fit at all" (bound > pool_limit): refused, exactly, no
#     fraction — see `_err_402_does_not_fit`. Not claimed as an acceptance
#     criterion for the strict-only first slice (section 12): this and
#     ordinary exhaustion may share one refusal there.
#   - "will monopolise" (bound exceeds 1/N of pool_limit while still fitting
#     under it): NOT refused — it can legitimately succeed — but WARNED,
#     naming the tenant and the ratio, because it means the budget is sized
#     for fewer concurrent requests than the workload wants. Also NOT claimed
#     for the first slice (criterion 3 is explicitly deferred).
#   - "refused while mostly unused": every refusal logs the reserved/settled
#     split at that moment (see the refusal sites below) — this IS part of
#     the first slice (it costs nothing extra and the aggregate-visibility
#     gap it closes is real from day one).
#
# `N` is "the minimum in-flight concurrency the tenant is sized for" — a
# TENANT setting per the contract, defaulting to 100 because, per the
# contract's own account, both independent reviewers landed near that figure.
# This module keeps that default as a single global env-configurable value
# rather than a new per-tenant Tenants-table attribute: criterion 3 (the only
# one this constant serves) is explicitly OUT of the first slice being shipped
# (section 12), so per-tenant override plumbing — mirroring `bound_mode`'s — is
# left as follow-up work rather than built ahead of the criterion that needs
# it. Recorded here as a documented simplification, not silently done.
_DEFAULT_MONOPOLISE_WARN_N = 100
_MAX_RESERVATION_FRACTION_OF_POOL = 1.0 / float(
    os.getenv("STRATOCLAVE_MONOPOLISE_WARN_N", str(_DEFAULT_MONOPOLISE_WARN_N))
)

_RESERVE_MAX_RETRIES = 12
_RESERVE_BACKOFF_SECONDS = 0.01  # base delay for the exponential backoff below.
_RESERVE_BACKOFF_CAP_SECONDS = 0.4  # ceiling so a hot row can't stall a request.
# Settlement must not fail a live request; it retries a few times against
# transient capacity errors before giving up loudly (a lost settle leaks the
# hold, so it is logged at error level for reconciliation).
_SETTLE_MAX_RETRIES = 4
#: A release cancelled by contention is retried this many times before the
#: reservation is left to the expired-hold sweep.
_RELEASE_MAX_RETRIES = 3
# Settle runs at the tail of the STREAMING path (from run_stream's async
# generator, on the event loop), so its backoff sleep blocks every co-located
# stream. Settle contention is far rarer and less bursty than the reserve
# thundering-herd, so cap its jitter much tighter: worst case ~0.05s×retries
# instead of ~0.4s×retries.
_SETTLE_BACKOFF_CAP_SECONDS = 0.05


def _contention_backoff(attempt: int, cap: float = _RESERVE_BACKOFF_CAP_SECONDS) -> float:
    """Full-jitter exponential backoff for a hot single-row transaction.

    Linear backoff synchronises a thundering herd: every loser of an optimistic
    lock race sleeps the *same* interval and collides again on the next attempt,
    so a burst of concurrent reserves against one tenant's pool row exhausts all
    retries and fails closed (503). Exponential growth with full jitter spreads
    the retries across a widening window, so colliding writers desynchronise and
    the snapshot lock actually makes progress. `attempt` is 1-based (0 never
    backs off). Uses AWS's recommended full-jitter: sleep ∈ [0, min(cap, base*2^n)].
    `cap` lets the settle path use a tighter ceiling than the reserve path.
    """
    ceiling = min(cap, _RESERVE_BACKOFF_SECONDS * (2 ** attempt))
    return random.uniform(0, ceiling)

# Orphan-reservation reaper.
# --------------------------
# release_pool() hands a hold back on *handled* error paths, but a task kill /
# OOM / deploy drain can terminate the process between reserve and settle with
# neither running — leaking that request's share of `pool_reserved_microusd`
# forever (there is no server-side timer). To bound and self-heal that leak
# without adding any infrastructure, every pooled reservation writes a sibling
# HOLD row (in the same TransactWriteItems as the reserve) carrying its amount
# and an expiry; settle/release delete it; and each pooled reserve lazily sweeps
# a few *expired* holds, reclaiming their amount back into the aggregate.
#
# The TTL should exceed the longest realistic request (a slow extended-thinking
# stream, plus Bedrock throttling waits and settle backoff) so a still-running
# request is not mistaken for a crashed one. Even so, settle/release/reclaim are
# now written so an early reclaim can only *lose the reclaimer's own work* to the
# idempotency latch — it can never double-subtract reserved (see
# `hold_delete_txn_item`). The TTL is therefore a tuning knob for sweep timeliness,
# not the sole guarantor of money-safety. A hard floor stops a mis-set env var
# (e.g. a throwaway "60") from turning every in-flight hold into a false orphan.
_HOLD_TTL_FLOOR_SECONDS = 1800
_HOLD_TTL_SECONDS = max(
    int(os.getenv("STRATOCLAVE_POOL_HOLD_TTL_SECONDS", "3600")),
    _HOLD_TTL_FLOOR_SECONDS,
)

# docs/design/hard-ceiling.md section 5: "the reap timeout must exceed the
# maximum time a charge can still arrive for a hold — the request deadline
# plus the retry budget plus a margin for clock skew. Derive it from those
# values in code rather than choosing a constant, so a change to a timeout
# cannot silently invalidate it. Assert that relationship at startup."
#
# With the legacy estimate, an under-reservation was the only failure mode the
# reaper's timing had to out-live; with a sound bound, the reservation is HELD
# for the call's full duration and is several times larger, so a hold released
# while its call is still running is now a genuine ceiling breach: the
# returned headroom admits a second request, and when the first charge lands
# both are booked — a capacity leak, not a double-spend, but still a breach of
# "settled + reserved <= pool_limit at any ledger-reconstructible state".
#
# DERIVATION (not a chosen constant): for each upstream transport this
# pipeline's holds wait on, one attempt's own worst-case wall-clock time is
# (connect + read timeout) — the time the underlying HTTP client will wait
# before giving up and surfacing an error — multiplied by however many attempts
# can happen before control returns to this pipeline. A hold's charge can only
# "still arrive" for as long as ONE of these attempt-sequences is running, so
# the ceiling is the WORST (largest) of the transports this pipeline actually
# uses, plus a margin for clock skew between the process that started the timer
# and the one that later reads it. This imports the SAME named constants
# `_bedrock_clients`/`_openai_transport` configure their real clients with
# (added there for exactly this reason), so a change to either transport's
# timeout changes this derivation automatically instead of by someone
# remembering to update a second copy.
#
# WHERE THE ATTEMPTS LIVE changed, and the derivation follows it. This used to
# multiply by the Bedrock client's `RETRY_MAX_ATTEMPTS` on the belief that the
# SDK held the retry budget. Two things were wrong: that constant was configured
# through botocore's `max_attempts`, which means RETRIES (the real ceiling was
# one attempt higher than the derivation assumed), and the streaming path's own
# retry loop in `mvp.routing.infrarouter` was never counted at all. The SDK now
# makes exactly one attempt, so the retry budget is the router's: it starts no
# new attempt after `_CHAIN_DEADLINE_S`, and an attempt started just under that
# deadline can still run a full (connect + read). Hence chain deadline plus one
# attempt, rather than a multiple of attempts.
from ._bedrock_clients import (
    CONNECT_TIMEOUT_SECONDS as _BEDROCK_CONNECT_SECONDS,
    READ_TIMEOUT_SECONDS as _BEDROCK_READ_SECONDS,
    RETRY_MAX_ATTEMPTS as _BEDROCK_SDK_ATTEMPTS,
)
from ._openai_transport import (
    RETRY_MAX_ATTEMPTS as _openai_RETRY_ATTEMPTS,
    STREAM_READ_TIMEOUT_SECONDS as _openai_READ_SECONDS,
)
from .routing.infrarouter import CHAIN_DEADLINE_SECONDS as _ROUTER_CHAIN_DEADLINE

_BEDROCK_ONE_ATTEMPT_SECONDS = (
    (_BEDROCK_CONNECT_SECONDS + _BEDROCK_READ_SECONDS) * _BEDROCK_SDK_ATTEMPTS
)
_BEDROCK_WORST_CASE_SECONDS = _ROUTER_CHAIN_DEADLINE + _BEDROCK_ONE_ATTEMPT_SECONDS
# the OpenAI-compatible endpoint's connect timeout (10s) is a module-private literal in
# `_openai_transport._DEFAULT_TIMEOUT`, not (yet) named — folding in only the
# named read timeout here is the conservative direction (it UNDERSTATES
# the OpenAI-compatible endpoint's worst case by the connect leg), which is safe for a floor this
# module then adds a full clock-skew margin on top of; overstating would be
# the unsafe direction.
_openai_WORST_CASE_SECONDS = _openai_READ_SECONDS * _openai_RETRY_ATTEMPTS

# Clock-skew margin: this pipeline's own timestamps (`created_at` on the hold,
# `expires_at`) are all server-side wall-clock, but the reaper's sweep and the
# request that wrote the hold can run on DIFFERENT hosts/containers. AWS's own
# guidance keeps NTP-synchronised hosts within low single-digit seconds of
# each other; 60s is a generous, round multiple of that with margin for a
# host whose NTP sync has degraded, not a number tuned to any test.
_CLOCK_SKEW_MARGIN_SECONDS = 60

REQUEST_DEADLINE_SECONDS = max(_BEDROCK_READ_SECONDS, _openai_READ_SECONDS)
RETRY_BUDGET_SECONDS = max(_BEDROCK_WORST_CASE_SECONDS, _openai_WORST_CASE_SECONDS)
# `MAX_CALL_DURATION_SECONDS` is named to match section 5's own vocabulary
# ("the request deadline plus the retry budget plus a margin for clock skew")
# — `RETRY_BUDGET_SECONDS` here already folds the deadline into its own
# worst-case product (attempts x (connect+read)), so the sum below double-
# counts one attempt's deadline on top of the retry product. That is
# deliberate slack in the SAFE direction (the ceiling can only be an
# over-estimate of "how long a charge can still arrive"), not a bug: a
# genuinely tight derivation would need per-transport attempt counts carried
# separately through the max() above, which is more machinery than this
# constant's one consumer (the assertion below) justifies today.
MAX_CALL_DURATION_SECONDS = (
    REQUEST_DEADLINE_SECONDS + RETRY_BUDGET_SECONDS + _CLOCK_SKEW_MARGIN_SECONDS
)

if _HOLD_TTL_FLOOR_SECONDS <= MAX_CALL_DURATION_SECONDS:
    raise RuntimeError(
        "hard-ceiling invariant violated at import: _HOLD_TTL_FLOOR_SECONDS "
        f"({_HOLD_TTL_FLOOR_SECONDS}s) must exceed MAX_CALL_DURATION_SECONDS "
        f"({MAX_CALL_DURATION_SECONDS}s = request deadline "
        f"{REQUEST_DEADLINE_SECONDS}s + retry budget {RETRY_BUDGET_SECONDS}s + "
        f"skew margin {_CLOCK_SKEW_MARGIN_SECONDS}s), or the reaper can "
        "reclaim a hold whose call is still legitimately running."
    )
if _HOLD_TTL_SECONDS <= MAX_CALL_DURATION_SECONDS:
    # The floor passed; the CONFIGURED value (env override) did not. Fail at
    # import — same reasoning as the floor check, but on the value that is
    # actually in force — rather than let a misconfigured deploy silently run
    # with a TTL shorter than a call it will happily allow. This is
    # acceptance criterion 8: "startup fails when the reap timeout does not
    # exceed the request deadline plus the retry budget plus the skew margin."
    raise RuntimeError(
        "hard-ceiling invariant violated at import: STRATOCLAVE_POOL_HOLD_TTL_SECONDS "
        f"resolves to {_HOLD_TTL_SECONDS}s, which does not exceed "
        f"MAX_CALL_DURATION_SECONDS ({MAX_CALL_DURATION_SECONDS}s)."
    )
# Reclaim only a handful of expired holds per request so the sweep never turns
# the hot reserve path into an unbounded scan.
_SWEEP_MAX_HOLDS = int(os.getenv("STRATOCLAVE_POOL_SWEEP_MAX_HOLDS", "5"))

# Two-item migration (docs/design/ledger-hot-path.md step 3): capture/void reads
# the HOLD row ALONE (source/amount/rate_snapshot folded onto it) instead of the
# soon-async RESERVE event. The cutover is NOT a bare flag (Fable review-2 finding
# 2, CONFIRMED data-loss hazard): a blunt flip would route a pre-enrichment
# external hold still inside its TTL to the HOLD-only path where its `source` is
# absent → 404 → the caller declines an ALREADY-AUTHORIZED + reserved transaction
# → the reaper voids it. Instead the path is decided per-hold by an ENRICHMENT
# EPOCH: only holds minted AT OR AFTER the epoch take HOLD-only; older holds keep
# the RESERVE-event fallback (safe by construction — the fallback is deleted only
# after epoch + max hold TTL, in a SEPARATE deploy gated on the reconciler's
# post-epoch-source-less count being zero for that whole window).
#
# The epoch MUST be the time the enrichment deploy finished rolling out to EVERY
# instance (NOT deploy start): a clock-forward hold minted by an old-code instance
# before enrichment could otherwise look post-epoch, have no `source`, and 404 —
# reintroducing the hazard. Set it conservatively LATE (finish time + an NTP-skew
# margin); too-late only keeps benign holds on the (still-correct) fallback, while
# too-early is the only setting that loses money.


def _parse_epoch_env(raw: Optional[str]) -> Optional[float]:
    """Parse STRATOCLAVE_ENRICHMENT_EPOCH → epoch seconds (float, UTC), or None if
    unset. Accepts an epoch-seconds number or an ISO-8601 timestamp. FAIL-FAST on
    an unparseable value (Fable review): silently treating a typo as None would
    leave the process permanently on step-2 behaviour after an operator BELIEVED
    they cut over — safe money-wise but an undetectable no-op. A naive ISO string
    is assumed UTC so the comparison never mixes naive/aware datetimes."""
    if raw is None or raw.strip() == "":
        return None
    raw = raw.strip()
    try:
        return float(raw)  # epoch seconds
    except ValueError:
        pass
    from datetime import datetime, timezone

    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _hold_created_epoch(hold: dict) -> Optional[float]:
    """A HOLD row's `created_at` (ISO-8601 string) → epoch seconds (UTC), or None
    if absent/unparseable. A naive string is assumed UTC to match
    `_parse_epoch_env`; None routes the hold to the pre-epoch (legacy) path, which
    is the fail-closed / money-safe side during migration."""
    raw = hold.get("created_at")
    if not raw:
        return None
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _marker_created_epoch(marker: dict) -> Optional[float]:
    """A separate-item marker's `created_at` (ISO-8601) → epoch seconds, or None.
    Used by the reconcile audit sweep to require an orphan marker be OLDER than the
    max hold lifetime before settling it, so a marker whose owning hold row is
    merely lagging our read is never mistaken for an orphan (Fable PR-1 review Bug
    1). Same parsing as `_hold_created_epoch`."""
    return _hold_created_epoch(marker)


# Max lifetime of a hold, from the authorize TTL clamp (_TTL_MAX_SECONDS = 24h in
# billing_authorize) plus a margin for the reaper's one-previous-period reach and
# clock skew. The reconcile audit sweep only settles a RESERVED marker with NO
# owning hold row once the marker is older than this — beyond it, no live hold
# could still claim the marker, so an ownerless marker is a genuine post-terminal
# storage orphan. Kept as a local constant (not imported) to avoid a
# billing_authorize import cycle.
_AUTHORIZE_MAX_TTL_SECONDS = 24 * 60 * 60           # mirrors billing_authorize._TTL_MAX_SECONDS
_MARKER_ORPHAN_MARGIN_SECONDS = 60 * 60             # 1h slack for reaper/clock skew


# Parsed once at import; an unparseable value raises here (fail-fast at boot).
_ENRICHMENT_EPOCH = _parse_epoch_env(os.getenv("STRATOCLAVE_ENRICHMENT_EPOCH"))

# PENDING protocol selector (docs/design/pending-protocol.md). Default
# "transaction" = today's 4-item TransactWriteItems, byte-for-byte unchanged.
# "pending" = the non-transactional 3-write hot path (Put PENDING -> single
# conditional UpdateItem commit -> async ACTIVE) that the spikes proved cuts the
# c=16 p99 from ~1,190 ms to ~168 ms. Flipping this is a per-tenant canary that,
# per the design doc, is gated on live observation windows — so it ships OFF and
# the readers were taught `status` (absent == ACTIVE) first, making the whole
# path inert until deliberately enabled.
_RESERVE_PROTOCOL = os.getenv("STRATOCLAVE_RESERVE_PROTOCOL", "transaction").lower()

# Per-tenant canary allowlist (docs/design/pending-protocol.md, rollout
# Shadow->Canary->Full). A comma-separated set of tenant ids that use the PENDING
# protocol EVEN WHEN the global default is "transaction", so a single tenant can be
# flipped without a global switch. Parsed ONCE at import (no per-request I/O — the
# hot path must not add a lookup). The global flag still wins when it is "pending"
# (all tenants). Precedence: global=="pending" OR tenant in allowlist -> pending.
_RESERVE_PROTOCOL_TENANTS = frozenset(
    t.strip() for t in os.getenv("STRATOCLAVE_RESERVE_PROTOCOL_TENANTS", "").split(",")
    if t.strip()
)


def _reserve_protocol_for(tenant_id: Optional[str]) -> str:
    """Resolve the reserve protocol for a tenant: "pending" if the global flag is
    "pending" OR this tenant is in the canary allowlist, else "transaction". This is
    the SINGLE decision point every reserve/settle/reclaim/capture marker branch
    consults, so a canary tenant is byte-consistent across its whole lifecycle
    (reserve writes a marker ⇒ settle/reclaim must clean it up even if the global
    flag never flipped). Marker cleanup is money-neutral when no marker exists, so a
    tenant removed from the allowlist mid-flight still settles cleanly."""
    if _RESERVE_PROTOCOL == "pending":
        return "pending"
    if tenant_id and tenant_id in _RESERVE_PROTOCOL_TENANTS:
        return "pending"
    return "transaction"


def pool_deltas(reserved_microusd, actual_microusd):
    """The three counter deltas a pool move applies, as a pure function.

    Extracted from `_pool_settle_items` so the arithmetic can be verified over
    symbolic values rather than over a transcription of it. Two reviews landed on
    the same finding: a Z3 proof that re-implements this in the test file proves the
    test author's algebra, and stays green while production swaps two bindings.
    Nothing here converts to `int`, so z3 `Int` expressions flow through unchanged
    and the proof is over the shipped expression.

    Returns `(reserved_delta, settled_delta, headroom_delta)` for
    `ADD pool_reserved :dr, pool_settled :actual, pool_headroom :dh`. `headroom` is
    defined as `limit - reserved - settled`, so returning `reserved` and booking
    `actual` moves it by their difference.
    """
    return (-reserved_microusd, actual_microusd, reserved_microusd - actual_microusd)


def _pool_settle_items(
    *,
    table_name: str,
    tenant_id: str,
    period: str,
    reserved_microusd: int,
    actual_microusd: int,
    reclaimed_microusd: int = 0,
    hold_id: Optional[str] = None,
):
    """Build the single TransactWriteItems fragment that settles a pool hold.

    Kept here (rather than inline) so settle, the error-path release, and the
    reaper all compose the exact same aggregate update — moving `reserved` out
    of `pool_reserved` and `actual` into `pool_settled`.

    `attribute_exists(tenant_id)` gates the update: if the pool row was legit-
    imately deleted mid-flight (the `pool_vanished` path), an in-flight settle
    or reclaim must NOT resurrect it as a ghost row carrying a negative
    `pool_reserved` (which a later `set_pool_limit` would preserve, inflating the
    next period's effective budget). A cancelled update is a no-op: no row means
    no reservation to reconcile.

    `reclaimed_microusd` (reaper only) records orphan value returned without
    spend, so operators can reconcile against the Bedrock bill for the rare case
    where the crash happened *after* a successful model call.
    """
    from dynamo.tenant_budgets import budget_sk

    # headroom = limit - reserved - settled, so releasing `reserved` and adding
    # `actual` of true spend shifts headroom by (reserved - actual). This keeps
    # the invariant on settle (actual>0), release, and reclaim (both actual=0 =>
    # full reservation returned to headroom). Same aggregate for all three paths.
    d_reserved, d_settled, delta_headroom = pool_deltas(
        reserved_microusd, actual_microusd
    )
    expr = ("ADD pool_reserved_microusd :dr, pool_settled_microusd :actual, "
            "pool_headroom_microusd :dh")
    values = {
        ":dr": {"N": str(d_reserved)},
        ":actual": {"N": str(d_settled)},
        ":dh": {"N": str(delta_headroom)},
    }
    if reclaimed_microusd:
        expr += ", pool_reclaimed_microusd :rec"
        values[":rec"] = {"N": str(int(reclaimed_microusd))}
    # NOTE (docs/design/pending-protocol.md, PR-1): the per-hold marker is NO LONGER
    # an `applied.<hold_id>` map entry on this pool item, so this fragment no longer
    # REMOVEs one. The marker now lives in a SEPARATE item and is transitioned
    # RESERVED -> SETTLED (with a TTL stamp) by a companion transaction item that
    # the settle/release assembly appends (see `hold_id`-driven
    # `marker_credit_back_txn_item`). `hold_id` is accepted for signature
    # compatibility with the transaction-mode callers but is not used here.
    _ = hold_id
    item: dict = {
        "TableName": table_name,
        "Key": {
            "tenant_id": {"S": tenant_id},
            "sk": {"S": budget_sk(period)},
        },
        "UpdateExpression": expr,
        "ConditionExpression": "attribute_exists(tenant_id)",
        "ExpressionAttributeValues": values,
    }
    return {"Update": item}


@dataclass
class ReservationContext:
    """Carries everything settle needs to reconcile a reservation.

    Returned by `reserve_credit()` and passed back into
    `settle_reservation_and_log()`. Backward compatible with call sites that
    only kept the `UserTenantsRepository`: `tenants_repo` remains an attribute
    and the object is also duck-usable wherever the repo was expected for the
    `reserve()`/`refund()`/`get()` calls the settle path made.
    """

    tenants_repo: UserTenantsRepository
    reservation_tokens: int
    pool_reserved_microusd: int = 0
    period: Optional[str] = None
    pricing_key: Optional[str] = None
    # Layer 5: the exact rate this reservation was admitted at, frozen at reserve
    # time. settle/late-settle rate the charge from THIS snapshot (a pure fn, no
    # live-table re-read), so a rate flip between reserve and settle cannot change
    # the price. Serialized onto the RESERVE ledger event; None only for
    # non-priced/legacy reservations.
    rate_snapshot: Optional["RateSnapshot"] = None
    tenant_id: str = ""
    pool_active: bool = False
    quota_lines: list = None  # list[dict] of per-model quota txn items (None = no quota)
    # Per-model quota bookkeeping (set when a quota reservation was committed),
    # so settle/release can move the same model's `used` counter. `selected_model`
    # is the model the cascade actually landed on (may differ from requested).
    selected_model: Optional[str] = None
    # `requested_model` is what the client asked for (body.model, pre-cascade),
    # kept so settle can record P0-11 fallback visibility. Stamped for EVERY
    # request by `reserve_credit_for_model` (the single reserve chokepoint all
    # three handlers go through — verified: no handler calls bare
    # `reserve_credit`), so a live row never has this None. It defaults None
    # only for defensively-constructed contexts / tests.
    requested_model: Optional[str] = None
    quota_reserved_amount: int = 0
    quota_user_id: Optional[str] = None
    # The period the quota `used` counter was reserved against. settle/release
    # MUST key off this, never a fresh current_period() — a long request (or a
    # stream) that crosses a month boundary would otherwise settle the wrong
    # period's row (leaking the reserved period, negative-seeding the new one).
    quota_period: Optional[str] = None
    quota_tenant_limit: Optional[int] = None
    quota_user_limit: Optional[int] = None
    # Hard-ceiling reservation bound (docs/design/hard-ceiling.md item 4): the
    # inputs the pool debit's `cost_microusd` was actually computed from, kept
    # so the SETTLE ledger terminal can carry a RECOMPUTABLE reservation rather
    # than an opaque number. `bound_mode` is None when the legacy
    # `estimate_cost_microusd` heuristic priced this reservation — either a
    # caller that has not yet been migrated to pass `input_bytes` (see
    # `reserve_credit_for_model`), OR a `shadow_mode` reservation (a sound
    # bound WAS computed, but `_legacy_estimate` is what actually got
    # reserved) — so a dispute can tell "priced under the old heuristic"
    # apart from "priced under a sound/calibrated bound" using the ONE
    # question this field answers: what strategy produced `cost_microusd`.
    # It is NOT "was a bound computed at all" — see `measured_bound_microusd`
    # for that, which can be populated while this stays None.
    bound_mode: Optional[str] = None
    # Section 3a: the canonical outbound payload's byte length (text only —
    # section 3b's split means this deliberately EXCLUDES image payload
    # bytes, which are covered by the dimension term instead) and its hash,
    # pinned at reserve time. Acceptance criterion 9 compares these against an
    # independent capture of the bytes actually sent, so both must be computed
    # from the SAME canonical payload the client library serialises, not from
    # the request body the route received.
    reserved_input_bytes: Optional[int] = None
    reserved_payload_hash: Optional[str] = None
    reserved_extra_input_tokens: int = 0
    reserved_max_output_tokens: Optional[int] = None
    reserved_effort_multiplier: int = 1
    # The coordinator's ITEM 2 (a per-request `measured` destination): the
    # amount `cost_microusd` this reservation was priced at, carried on the
    # context so `settle_reservation_and_log` can hand it to
    # `UsageLogsRepository().record()` regardless of whether a dollar pool
    # exists. This is what gives the `measured` state (no pool, but the
    # bound WAS computed because the measurement flag is on) a place to land:
    # the usage row is already per-request and append-only, so recording
    # this here needs no shared-item write and pollutes no ledger invariant —
    # explicitly NOT a synthesised pool row or a fake RESERVE event. Set
    # whenever `cost_microusd` was supplied to `reserve_credit`, regardless of
    # `pool_active` — for an `enforced` reservation this duplicates
    # `pool_reserved_microusd` (harmless: a cheap cross-check between the
    # ledger and the usage log), and for a `measured` one it is the ONLY
    # place the computed bound survives at all.
    #
    # `shadow` (a pool exists, the gate flag is off — see
    # `reservation_bound.dollar_pool_bound_should_gate`) is the one state
    # where this field DIVERGES from `pool_reserved_microusd`: admission
    # reserves the legacy `estimate_cost_microusd` amount (byte-for-byte as
    # if the hard-ceiling bound had never shipped — see
    # `reserve_credit_for_model`'s `shadow_mode`), but the whole purpose of
    # shadow mode is to compare the sound bound against what settle actually
    # charges, so this field is deliberately still the BOUND, not the
    # reserved amount, in that state. `reserve_credit`'s `bound_microusd`
    # parameter is what carries that distinction down from `_price`; when
    # omitted this field defaults to `cost_microusd`, i.e. bound and reserved
    # coincide, exactly as they always have outside `shadow`.
    measured_bound_microusd: Optional[int] = None
    # Attribution carried from the request headers (x-sc-*), stamped at the
    # reserve chokepoint. NOT money — used so settle can key the ledger event's
    # run-index (gsi1pk) on the client's workflow_run_id, making per-run billing
    # (GET /billing/runs/<workflow_run_id>) queryable. Absent → the ledger falls
    # back to the hold_id (run_id_is_fallback=True) as before.
    workflow_run_id: Optional[str] = None
    group_id: Optional[str] = None
    request_id: Optional[str] = None
    # Reservation origin. "external" marks a hold created by the external
    # authorize/capture API (not an inline LLM request). It changes exactly ONE
    # money behaviour: on a settle that loses the terminal race to a reaper
    # RECLAIM, an external hold must NOT be recovered via LATE_SETTLE (an
    # external capture window is tenant-controlled and unbounded, so late-billing
    # a reclaimed hold could break the budget invariant — Fable authcap D-2).
    # Instead the settle signals `ExternalHoldReclaimed` so the capture endpoint
    # returns 410. None/"" = an ordinary inline reservation (unchanged behaviour).
    source: Optional[str] = None
    # Routing decision facts captured at reserve (P0 decision log): the chosen
    # candidate + the rejected candidates with per-candidate estimate + reason.
    # Pure attribution — the handler emits it fire-and-forget; None when routing
    # had no real choice (single-candidate / no-config passthrough).
    decision_facts: Optional[dict] = None
    # The external-VSR consult decision for this request, when the VSR feature
    # acted: {decision, suggested_model, mode, config_version}. Observability
    # only — NEVER money. Carried onto the reserve-time decision record so an
    # offline job can join "VSR advised X" against the committed/billed model by
    # span_id. None for every non-VSR request (dark ship).
    vsr_decision: Optional[dict] = None
    # This reservation's HOLD row identity. `hold_sk` is the FULL sort key
    # (`HOLD#<period>#<expires_at:010d>#<hold_id>`) — the expiry is embedded so
    # the reaper can range-scan by expiry, so settle/release must delete by the
    # exact SK they hold, not reconstruct it from hold_id. The reaper reclaims
    # holds whose owning request died before settle/release; settle and release
    # delete this hold in the same transaction that adjusts the aggregate.
    hold_id: Optional[str] = None
    hold_sk: Optional[str] = None
    # Guards the pool hold against being released or settled twice. A single
    # request settles OR releases its pool reservation exactly once; both paths
    # flip this so a defensive double-call (e.g. an error handler plus the
    # streaming `finally`) cannot drive pool_reserved negative.
    _pool_finalized: bool = field(default=False, repr=False)

    def _retry_release(self, items) -> None:
        """Re-attempt a release that was cancelled by contention, not by a condition.

        Bounded and best-effort, like the release itself: the caller is already on an
        error path and must not be made to wait indefinitely or to raise. What this
        buys is that a hot pool row no longer costs a tenant its headroom until the
        sweep runs — the reservation is returned now, in the common case, and the
        failure is named when it is not.
        """
        client = _low_level_client()
        for attempt in range(1, _RELEASE_MAX_RETRIES + 1):
            time.sleep(_contention_backoff(attempt))
            try:
                client.transact_write_items(
                    TransactItems=items,
                    ClientRequestToken=_fresh_idempotency_token(),
                )
                logger.info(
                    "pool_release_recovered_after_contention",
                    tenant_id=self.tenant_id,
                    period=self.period,
                    attempt=attempt,
                )
                return
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code != "TransactionCanceledException":
                    break
                reasons = [
                    str((r or {}).get("Code") or "")
                    for r in (e.response.get("CancellationReasons") or [])
                ]
                if not any(r == "TransactionConflict" for r in reasons):
                    # A condition failed on this attempt: the hold went terminal
                    # while we were retrying, which is the benign outcome.
                    return
            except Exception:  # noqa: BLE001 — best-effort, never mask the original
                break
        logger.error(
            "pool_release_still_held_after_retries",
            tenant_id=self.tenant_id,
            period=self.period,
            reserved_microusd=self.pool_reserved_microusd,
            note="the reservation stays outstanding until the expired-hold sweep",
        )

    def release_pool(self) -> None:
        """Release this request's outstanding pool reservation without recording
        spend (actual settled = 0).

        Called on error paths where Bedrock produced no billable usage — the
        upfront `pool_reserved_microusd` must be handed back to the pool or it
        leaks forever (there is no reaper). Idempotent and best-effort: a failed
        release is logged, never raised, so it cannot mask the original error.
        """
        if (
            not self.pool_active
            or self._pool_finalized
            or self.pool_reserved_microusd <= 0
            or self.period is None
        ):
            return
        self._pool_finalized = True
        budgets = TenantBudgetsRepository()
        # Return the reserved amount AND delete this hold in one transaction, so
        # the aggregate and the hold vanish together. The hold delete is gated on
        # `attribute_exists(sk)`: if the reaper already reclaimed this hold (a
        # slow error path that outlived the TTL), it ALSO already returned the
        # reserved amount — so the cancelled transaction correctly leaves the
        # aggregate untouched instead of subtracting `reserved` a second time.
        _rel_hold_id = self.hold_id
        if not _rel_hold_id and self.hold_sk:
            _rel_hold_id = self.hold_sk.rsplit("#", 1)[-1] or None
        items = [
            _pool_settle_items(
                table_name=budgets.table_name,
                tenant_id=self.tenant_id,
                period=self.period,
                reserved_microusd=self.pool_reserved_microusd,
                actual_microusd=0,
                hold_id=_rel_hold_id,   # REMOVE this hold's marker (PENDING protocol)
            )
        ]
        if self.hold_sk:
            items.append(
                budgets.hold_delete_txn_item(
                    tenant_id=self.tenant_id, sk=self.hold_sk
                )
            )
        # Phase 2: record a RELEASE terminal in the SAME txn as the reserved
        # return, so the reserved side is ledger-derivable (I2) and RELEASE shares
        # the single TERMINAL sk with SETTLE/RECLAIM. attribute_not_exists makes
        # release mutually exclusive with a racing reaper RECLAIM: if the reaper
        # already wrote RECLAIM (and already returned reserved), this txn CCFs and
        # cancels — correctly leaving the counter untouched (the existing
        # TransactionCanceled handler treats it as already-reconciled).
        _rel_hold_id = self.hold_id
        if not _rel_hold_id and self.hold_sk:
            _rel_hold_id = self.hold_sk.rsplit("#", 1)[-1] or None
        if _rel_hold_id:
            items.append(
                _reaper_ledger().terminal_event_txn_item(
                    tenant_id=self.tenant_id,
                    period=self.period,
                    hold_id=_rel_hold_id,
                    event_type="RELEASE",
                    reserved_delta_microusd=-int(self.pool_reserved_microusd),
                    settled_delta_microusd=0,
                    run_id=_rel_hold_id,
                    run_id_is_fallback=True,
                    settle_reason="release",
                    source=getattr(self, "source", None) or "inline",
                )
            )
        try:
            client = _low_level_client()
            client.transact_write_items(
                TransactItems=items,
                # Fresh per-call token: dedupes only botocore's transparent
                # retry of THIS release, never collides with a concurrent one.
                ClientRequestToken=_fresh_idempotency_token(),
            )
            # PENDING protocol (PR-1): the release above returned headroom + deleted
            # the hold atomically; settle the separate marker as cleanup (best-effort,
            # money-neutral — see the settle path).
            if _reserve_protocol_for(self.tenant_id) == "pending" and _rel_hold_id:
                budgets.marker_settle_best_effort(
                    tenant_id=self.tenant_id, hold_id=_rel_hold_id)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "TransactionCanceledException":
                # A cancellation says WHY in its reasons, and the two whys are
                # opposite. `ConditionalCheckFailed` means the hold is already
                # terminal — the reaper reclaimed it, or a settle got there — so
                # there is nothing left to release and the counter is correct.
                # `TransactionConflict` or throttling means nothing was written and
                # the reservation is STILL outstanding; reading that as
                # "already reconciled", which this did for every cancellation
                # equally, left the reservation held until the sweep reclaimed it,
                # with a log line that said the opposite.
                reasons = [
                    str((r or {}).get("Code") or "")
                    for r in (e.response.get("CancellationReasons") or [])
                ]
                retryable = {"TransactionConflict", "ThrottlingError",
                             "ThrottlingException", "ProvisionedThroughputExceeded",
                             "RequestLimitExceeded"}
                if any(r in retryable for r in reasons):
                    logger.warning(
                        "pool_release_contended",
                        tenant_id=self.tenant_id,
                        period=self.period,
                        reserved_microusd=self.pool_reserved_microusd,
                        reasons=",".join(reasons),
                    )
                    self._retry_release(items)
                else:
                    logger.info(
                        "pool_release_noop_already_reconciled",
                        tenant_id=self.tenant_id,
                        period=self.period,
                        reserved_microusd=self.pool_reserved_microusd,
                        reasons=",".join(reasons),
                    )
            else:
                logger.warning(
                    "pool_release_failed",
                    tenant_id=self.tenant_id,
                    period=self.period,
                    reserved_microusd=self.pool_reserved_microusd,
                    error_code=code,
                )
        except Exception:
            # A non-ClientError (e.g. botocore ReadTimeoutError, which is NOT a
            # ClientError) must never escape a best-effort release and mask the
            # original error that sent us down this path.
            logger.warning(
                "pool_release_failed",
                tenant_id=self.tenant_id,
                period=self.period,
                reserved_microusd=self.pool_reserved_microusd,
                error_code="non_client_error",
            )

    # --- UserTenantsRepository delegation ---------------------------------
    # Call sites historically held the `UserTenantsRepository` that
    # reserve_credit() returned and called `.refund()`/`.reserve()`/`.get()`
    # on it directly (e.g. to unwind a reservation when a stream errors before
    # settle). Delegating those methods keeps every existing call site working
    # unchanged whether or not a pool budget is in play.
    def refund(self, **kwargs):
        return self.tenants_repo.refund(**kwargs)

    def reserve(self, **kwargs):
        return self.tenants_repo.reserve(**kwargs)

    def get(self, *args, **kwargs):
        return self.tenants_repo.get(*args, **kwargs)

    def remaining_credit(self, *args, **kwargs):
        return self.tenants_repo.remaining_credit(*args, **kwargs)


_LOW_LEVEL_CLIENT = None


def _low_level_client():
    # Reuse one low-level client across requests: boto3 client construction is
    # not cheap (it builds the endpoint, signer, and event system each time).
    # The client is thread-safe for calls, and the region is fixed per process.
    global _LOW_LEVEL_CLIENT
    if _LOW_LEVEL_CLIENT is None:
        region = os.getenv("AWS_REGION", "us-east-1")
        _LOW_LEVEL_CLIENT = boto3.client("dynamodb", region_name=region)
    return _LOW_LEVEL_CLIENT


def _reset_low_level_client() -> None:
    """Test hook: drop the cached client so a new moto region takes effect."""
    global _LOW_LEVEL_CLIENT
    _LOW_LEVEL_CLIENT = None


def _fresh_idempotency_token() -> str:
    """A fresh ClientRequestToken (a 36-char UUID) for one TransactWriteItems call.

    DynamoDB dedupes retries carrying the *same* token for ~10 minutes, so
    botocore's transparent retry of a single call (after a lost ack) does not
    double-apply. Correctness therefore requires the token to be generated
    **once per logical call** — stable across botocore's internal retries of
    that call, but distinct across concurrent callers and across each iteration
    of our own explicit retry loops.

    It must NOT be derived from shared state (snapshot counters, amounts): under
    contention many callers read the same snapshot and would compute the same
    token, yet their transactions carry distinct `updated_at` values, so real
    DynamoDB rejects the collision with `IdempotentParameterMismatchException`.
    That failure mode is invisible under moto (which has no item-level
    transaction semantics) and only surfaces against real DynamoDB under load —
    which is exactly where it was found.
    """
    return str(uuid.uuid4())


# Namespace for deriving a stable-but-distinct token from a primary settle
# token. uuid5 keeps the result EXACTLY 36 chars (a raw UUID string), so it
# stays within DynamoDB's 36-char ClientRequestToken limit — unlike a naive
# f"{token}-so" (39 chars), which raises ValidationException on every call.
_SETTLED_ONLY_NS = uuid.UUID("5f2b9c14-0000-4000-8000-000000000001")


def _derived_token(primary: str, tag: str) -> str:
    """A 36-char ClientRequestToken deterministically derived from `primary`.

    Same `primary`+`tag` → same token, so a lost-ack retry of the derived write
    dedupes instead of double-recording; different primaries → different tokens.
    """
    return str(uuid.uuid5(_SETTLED_ONLY_NS, f"{primary}:{tag}"))


def _sweep_expired_holds(budgets, tenant_id: str, period: str) -> int:
    """Reclaim expired pool holds for `tenant_id`, this period AND the previous.

    A hold outlives its reservation only when the owning request's process died
    between reserve and settle (kill / OOM / drain) — settle and release delete
    the hold in the same transaction that adjusts the aggregate. This is the
    self-healing counterpart: each pooled reserve reclaims a few holds whose
    embedded expiry has passed, moving their amount back out of
    `pool_reserved_microusd` so a crash cannot permanently strand budget.

    The previous period is swept too: a crash in a month's final moments would
    otherwise strand that hold forever (this period's sweep only looks at this
    period's prefix, and native TTL is intentionally unused). One extra bounded
    range query per reserve is cheap.

    Best-effort and never raises: bounded to `_SWEEP_MAX_HOLDS` reclaims per
    call and EVERY exception (not just ClientError — botocore ReadTimeoutError
    is not a ClientError) is swallowed after logging, because a struggling
    reaper must never fail the live request that happens to be driving it.
    Returns the total count reclaimed (for tests / observability).
    """
    try:
        total = _sweep_one_period(budgets, tenant_id, period, _SWEEP_MAX_HOLDS)
        if total < _SWEEP_MAX_HOLDS:
            total += _sweep_one_period(
                budgets, tenant_id, _previous_period(period),
                _SWEEP_MAX_HOLDS - total,
            )
        return total
    except Exception:  # noqa: BLE001 — a reaper must never fail the live request
        logger.warning("pool_sweep_failed", tenant_id=tenant_id, period=period)
        return 0


class RetainedHoldNotFound(Exception):
    """No retained hold with that id for this tenant/period."""


class RetainedResolutionRaced(Exception):
    """The hold stopped being RETAINED between the read and the write."""


def list_retained_holds(tenant_id: str, period: str, *, limit: int = 100) -> list[dict]:
    """The reservations this tenant/period is holding back, for an operator to act on.

    A retained hold has no ending yet, by design, so nothing else surfaces it and
    nothing will resolve it on its own. That is the trade the retention makes: the
    budget is not handed back for a call that may have been billed, and in exchange
    somebody has to decide what it cost. This is the list they decide from."""
    return TenantBudgetsRepository().list_retained_holds(
        tenant_id=tenant_id, period=period, limit=limit)


def resolve_retained_hold(
    tenant_id: str, period: str, hold_id: str, *,
    charge_microusd: Optional[int] = None, release: bool = False,
) -> tuple[str, int]:
    """End a retained reservation, at a figure an operator supplies or at nothing.

    Returns `(terminal, charged_microusd)`. Exactly one of the two arguments is
    meaningful and the caller must choose: `charge_microusd` says "the provider's own
    record shows this much", `release=True` says "the provider's record shows no
    charge". The parameter is named for the provider's charge rather than for the
    pool's counter, because it is neither computed by nor checked against this
    gateway — it is testimony being entered into the ledger. The gateway cannot pick between them — that is why the retention exists
    — so there is no default here and no inference.

    Both branches go through the SAME money primitives the request path uses:
    the settle is `_settle_pool_side`, which is where the reserved→settled move, the
    hold delete and the terminal event live in one transaction with all their race
    reconciliation; the release is `ReservationContext.release_pool`. Nothing about
    the money is re-implemented for the admin path, so a retention resolves exactly
    as a request would have.

    An amount ABOVE what was reserved is refused rather than clamped. The reservation
    is the amount the pool actually has held; settling above it would push the settled
    side past what admission ever checked, and an operator who has a larger figure
    from the provider is reporting an overrun, which is a different record.
    """
    if (charge_microusd is None) == (not release):
        raise _err_400("resolve_requires_exactly_one_of_settled_or_release")
    budgets = TenantBudgetsRepository()
    ledger = _reaper_ledger()
    retained = ledger.get_retained(
        tenant_id=tenant_id, period=period, hold_id=hold_id)
    holds = [
        h for h in budgets.list_retained_holds(
            tenant_id=tenant_id, period=period, limit=500)
        if str(h.get("hold_id", "")) == hold_id
    ]
    if not holds:
        raise RetainedHoldNotFound(hold_id)
    hold = holds[0]
    amount = int(hold.get("amount_microusd", 0))
    actual = 0 if release else int(charge_microusd or 0)
    if actual > amount:
        raise _err_400("settled_exceeds_retained_reservation")

    # There is deliberately NO status write before the money moves. Flipping the hold
    # out of RETAINED first left a window: a crash between the two writes leaves an
    # expired ACTIVE hold, which the reaper reclaims on its own — the exact thing
    # retaining it was for. The settle and release primitives are status-agnostic
    # (their hold delete is conditioned on the row existing, not on what it says), so
    # the terminal cell is the only arbiter needed, and it is one the money
    # transaction already carries. Two operators racing therefore resolve the way two
    # racing settles do, and the loser is detected by reading what actually landed.
    ctx = ReservationContext(
        tenants_repo=UserTenantsRepository(),
        reservation_tokens=0,
        pool_reserved_microusd=amount,
        period=period,
        tenant_id=tenant_id,
        pool_active=True,
        hold_id=hold_id,
        hold_sk=str(hold.get("sk", "")),
        selected_model=str(hold.get("model_id") or "") or None,
    )
    if release:
        ctx.release_pool()
        return _resolution_outcome(ledger, tenant_id, period, hold_id,
                                   expected="RELEASE", expected_amount=0)
    _settle_pool_side(
        _RetentionActor(tenant_id), ctx, actual,
        ledger_facts={
            "model_id": str(hold.get("model_id") or "") or None,
            "pricing_version": None,
            "pricing_key": None,
            "rating": None,
            # Named so the ledger says WHY a charge arrived long after the request,
            # and at a figure nobody in the request path computed.
            "settle_reason": "retention_resolved",
            "run_id": str(hold.get("run_id") or "") or None,
            "source": str(hold.get("source") or "inline"),
            "retained_at": str(hold.get("retained_at") or "") or None,
            "attempt_marker": (retained or {}).get("attempt_marker"),
        },
    )
    return _resolution_outcome(ledger, tenant_id, period, hold_id,
                               expected="SETTLE", expected_amount=actual)


def _resolution_outcome(ledger, tenant_id: str, period: str, hold_id: str, *,
                        expected: str, expected_amount: int) -> tuple[str, int]:
    """Report the ending that actually landed, not the one this caller asked for.

    The money primitives reconcile a terminal clash as "already finalized" and return
    quietly, which is right for a request path — a request has nothing to tell anyone.
    An operator does: two people resolving one retention would otherwise both be told
    their own figure was recorded, and only one was. So the terminal is read back and
    a disagreement is raised rather than reported."""
    terminal = ledger.get_terminal(
        tenant_id=tenant_id, period=period, hold_id=hold_id)
    if terminal is None:
        # No ending at all: the money transaction did not commit and did not raise
        # (a throttle it swallowed). The retention is untouched and still resolvable.
        raise HTTPException(status_code=503, detail={
            "type": "retention_resolution_unavailable",
            "message": "The resolution did not commit. Retry."})
    landed = str(terminal.get("event_type", ""))
    landed_amount = int(terminal.get("settled_delta_microusd", 0))
    if landed != expected or landed_amount != int(expected_amount):
        raise RetainedResolutionRaced(
            f"{hold_id}: recorded {landed} at {landed_amount}, "
            f"this request asked for {expected} at {expected_amount}")
    return landed, landed_amount


class _RetentionActor:
    """Minimal principal for `_settle_pool_side`, which reads only `org_id`.

    A retention is resolved at the tenant level by an operator; there is no acting
    end-user whose token quota should move, and the audit trail for who did it is the
    admin audit event, not this object."""

    def __init__(self, tenant_id: str):
        self.org_id = tenant_id
        self.user_id = ""
        self.email = ""


def _retain_instead_of_returning(budgets, tenant_id: str, period: str,
                                  hold: dict, hold_id: str) -> bool:
    """Hold this reservation back rather than reclaiming it, if it should be.

    Three conditions, all of them facts rather than judgements:

      * the operator turned `STRATOCLAVE_UNOBSERVED_HOLDS` on. Off by default, so
        merging this changes no deployment's behaviour;
      * the hold records that a provider call departed (`provider_invoked_at`).
        Absence means the gateway never saw one leave, and returning the budget for
        a call that was never made is simply correct — this is the one place the
        distinction is worth money;
      * the hold is `inline`. An external authorization made no provider call at
        all, so it cannot have been billed by one.

    Returns whether the hold was retained. A False from the status write means
    something else ended the hold first, and then the caller must go on and reclaim
    normally rather than treat it as retained.
    """
    from . import provider_outcome as _outcome

    if not _outcome.unobserved_holds_enforced():
        return False
    if not hold.get("provider_invoked_at"):
        return False
    if str(hold.get("source") or "inline") != "inline":
        return False
    sk = str(hold.get("sk", ""))
    if not sk or not hold_id:
        return False
    try:
        if not budgets.hold_retain(tenant_id=tenant_id, sk=sk):
            return False
    except Exception:  # noqa: BLE001 — a failed retain falls through to the reclaim.
        logger.warning("pool_hold_retain_failed", tenant_id=tenant_id,
                       period=period, hold_id=hold_id)
        return False
    _reaper_ledger().put_retained(
        tenant_id=tenant_id, period=period, hold_id=hold_id,
        amount_microusd=int(hold.get("amount_microusd", 0)),
        attempt_marker=_outcome.attempt_request_metadata(
            hold_id, tenant_id).get("sc_attempt_id"),
        model_id=str(hold.get("model_id") or "") or None,
        provider_invoked_at=str(hold.get("provider_invoked_at") or "") or None,
        run_id=str(hold.get("run_id") or "") or None,
    )
    logger.error(
        "pool_hold_retained",
        tenant_id=tenant_id,
        period=period,
        hold_id=hold_id,
        amount_microusd=int(hold.get("amount_microusd", 0)),
        provider_invoked_at=str(hold.get("provider_invoked_at") or ""),
    )
    return True


def _redrive_owed_after_late_reclaim(*, budgets, ledger, tenant_id: str,
                                     period: str, hold_id: str) -> bool:
    """Recover an owed charge when the reclaim beat the owed row into existence.

    Called from the abandoned-settle path, immediately after that row is written. It
    is the mirror of the reaper's own check and exists only to make the pair total:
    the reaper checks after it commits, this checks after it writes, and a
    strongly-consistent read means the second of the two always sees the first.

    Never raises — the settle has already failed and its caller has already been
    answered — but the recovery it delegates to is the same at-most-once transaction,
    so running from both sides is safe rather than merely unlikely."""
    try:
        terminal = ledger.get_terminal(
            tenant_id=tenant_id, period=period, hold_id=hold_id)
    except Exception:  # noqa: BLE001 — a read failure leaves the row for the reaper.
        return False
    if not terminal or str(terminal.get("event_type", "")) != "RECLAIM":
        return False
    try:
        return _recover_owed_settle_after_reclaim(
            client=_low_level_client(), budgets=budgets, tenant_id=tenant_id,
            period=period, hold_id=hold_id)
    except Exception:  # noqa: BLE001 — logged inside; never fail the caller here.
        return False


def _recover_owed_settle_after_reclaim(*, client, budgets, tenant_id: str,
                                       period: str, hold_id: str) -> bool:
    """Post a charge the settle observed and could not commit, after its reclaim.

    Returns whether a recovery was attempted. Never raises: it runs inside the
    inline sweep, which must not be able to fail the request that happened to drive
    it — but every outcome is logged, because a recovery that silently does not
    happen is the defect this closes wearing a quieter face.
    """
    ledger = _reaper_ledger()
    try:
        owed = ledger.get_owed_settle(
            tenant_id=tenant_id, period=period, hold_id=hold_id)
    except Exception:  # noqa: BLE001 — a read failure is not "nothing owed".
        logger.warning("owed_settle_read_failed", tenant_id=tenant_id,
                       period=period, hold_id=hold_id)
        return False
    if not owed:
        return False
    actual = int(owed.get("settled_delta_microusd", 0))
    if actual <= 0:
        # An owed row for nothing is not a charge. Recorded rather than skipped
        # silently, because it would mean the settle path wrote a figure it should
        # not have.
        logger.warning("owed_settle_nonpositive", tenant_id=tenant_id,
                       period=period, hold_id=hold_id, actual_microusd=actual)
        return False
    facts = {
        k: owed.get(k) for k in (
            "span_id", "request_id", "group_id", "model_id", "pricing_version",
            "pricing_key", "tokens_in", "tokens_out")
        if owed.get(k) is not None
    }
    raw_rating = owed.get("rating")
    if raw_rating:
        import json as _json

        try:
            facts["rating"] = _json.loads(raw_rating)
        except Exception:  # noqa: BLE001 — a corrupt rating must not block the money.
            logger.warning("owed_settle_rating_unreadable", tenant_id=tenant_id,
                           period=period, hold_id=hold_id)
    for key in ("tokens_in", "tokens_out"):
        if key in facts:
            facts[key] = int(facts[key])
    try:
        _recover_spend_via_late_settle(
            client=client,
            ledger=ledger,
            budgets_table_name=budgets.table_name,
            tenant_id=tenant_id,
            period=period,
            hold_id=hold_id,
            actual_microusd=actual,
            run_id=str(owed.get("run_id") or hold_id),
            run_is_fallback=bool(owed.get("run_id_is_fallback", True)),
            facts=facts,
        )
    except Exception:  # noqa: BLE001 — the sweep is best-effort; the row survives.
        logger.error("owed_settle_recovery_failed", tenant_id=tenant_id,
                     period=period, hold_id=hold_id, actual_microusd=actual)
        return True
    logger.info("owed_settle_recovered", tenant_id=tenant_id, period=period,
                hold_id=hold_id, actual_microusd=actual)
    return True


def _reaped_hold_facts(hold: dict) -> dict:
    """What a reclaim must copy out of the hold row before deleting it.

    `docs/MEASUREMENTS.md`. The reaper credits an expired hold back with
    `actual=0`, which asserts the provider charged nothing. That assertion is
    false for any request that died after its bytes left: measured on real
    Bedrock, a call abandoned at a 2 s read timeout was billed 1,493 output
    tokens. Holding those reservations instead of returning them is a larger
    change (it needs a durable sweep cursor, pool-incarnation fencing, and a
    settle-dispatcher branch), and how big a problem it is worth solving should be
    a number rather than an argument.

    So this preserves the facts that make the number derivable from the ledger
    alone, because the reclaim's Delete destroys the row that holds them.
    `source` is the discriminator that already exists: "inline" means the hold
    backed a provider call, "external" means it backed an authorization that
    never made one, so only the inline sum is exposure at all.

    Read-only over the hold item, no new attribute, and nothing here is ever
    conditioned on — the money path is unchanged by design.
    """
    facts: dict[str, Any] = {}
    for key in ("source", "created_at", "provider_invoked_at"):
        val = hold.get(key)
        if val:
            facts[key] = str(val)
    for key in ("amount_microusd", "expires_at"):
        val = hold.get(key)
        if val is not None:
            try:
                facts[key] = int(val)
            except (TypeError, ValueError):
                pass
    return facts


def _sweep_one_period(budgets, tenant_id: str, period: str, cap: int) -> int:
    """Reclaim up to `cap` expired holds for one tenant/period. See
    `_sweep_expired_holds` for the contract; this is the per-period worker."""
    if cap <= 0:
        return 0
    reclaimed = 0
    retained = 0
    now_epoch = int(time.time())
    # The SK embeds the (zero-padded) expiry, so this range scan returns only
    # already-expired holds, oldest-expiry first, and `Limit` bounds it by
    # expiry — no filter, no risk of an orphan being buried behind live holds.
    # A small headroom over `cap` covers holds a concurrent sweep grabs first.
    expired = budgets.query_expired_holds(
        tenant_id=tenant_id,
        period=period,
        now_epoch=now_epoch,
        limit=cap + _SWEEP_MAX_HOLDS,
    )
    if not expired:
        return 0

    client = _low_level_client()
    for hold in expired:
        if reclaimed >= cap:
            break
        sk = str(hold.get("sk", ""))
        amount = int(hold.get("amount_microusd", 0))
        if not sk:
            continue
        # PENDING protocol (docs/design/pending-protocol.md, readers-first): this
        # reaper CREDITS BACK the reservation, so it must only touch holds whose
        # debit is known to have committed — ACTIVE, or the pre-PENDING implicit
        # ACTIVE (no status attribute). A PENDING hold may be un-debited, so
        # crediting it would oversell; those are handled by the sweeper's
        # `fence_pending_expired` (pool untouched) + the reconciler. This branch is
        # INERT for today's data (no hold carries `status`), so the reaper is
        # byte-identical until the flag is flipped.
        _status = hold.get("status")
        if _status is not None and str(_status) != "ACTIVE":
            continue
        if amount <= 0:
            # A zero/negative-amount hold ties up no budget; just delete it so it
            # stops being scanned (unconditional single-item delete, no aggregate
            # change). Best-effort.
            try:
                client.delete_item(
                    TableName=budgets.table_name,
                    Key={"tenant_id": {"S": tenant_id}, "sk": {"S": sk}},
                )
            except Exception:  # noqa: BLE001
                pass
            continue
        hold_id = str(hold.get("hold_id", ""))
        # C8.3: this reclaim is about to return the reservation and record a settled
        # delta of zero, which asserts the provider charged nothing. For a hold whose
        # provider call had already departed, that assertion is measurably false —
        # a Converse call abandoned at a 2 s read timeout was billed 1,493 output
        # tokens. With `STRATOCLAVE_UNOBSERVED_HOLDS` on, the reservation is HELD
        # instead: one conditional status write, no counter movement, so the amount
        # goes on being counted against the limit exactly as it already was. The
        # reaper skips a non-ACTIVE hold, so nothing offers to give it back again,
        # and the retention is resolved deliberately (admin: settle at the figure
        # from the provider's bill, or release when the bill shows nothing).
        if _retain_instead_of_returning(budgets, tenant_id, period, hold, hold_id):
            retained += 1
            continue
        try:
            _reaper_items = [
                _pool_settle_items(
                    table_name=budgets.table_name,
                    tenant_id=tenant_id,
                    period=period,
                    reserved_microusd=amount,
                    actual_microusd=0,
                    reclaimed_microusd=amount,
                    hold_id=hold_id or None,  # REMOVE marker (PENDING ACTIVE reclaim)
                ),
                budgets.reclaim_hold_txn_item(tenant_id=tenant_id, sk=sk),
            ]
            # Phase 2: write a RECLAIM terminal in the SAME txn as the counter
            # move + hold delete, so the ledger records the reserved return and a
            # racing settle that loses the terminal cell routes to LATE_SETTLE
            # (recovering the spend) instead of blind-returning it. The RECLAIM
            # shares the single TERMINAL sk with SETTLE/RELEASE, so
            # attribute_not_exists makes reaper-vs-settle mutually exclusive: if a
            # settle already wrote a terminal, the reaper's Put CCFs and the whole
            # reclaim txn cancels (no double return). hold_id is required; a legacy
            # hold row written before this deploy may lack it — skip the ledger
            # event (not the reclaim) so the counter is still healed.
            if hold_id:
                _reaper_items.append(
                    _reaper_ledger().terminal_event_txn_item(
                        tenant_id=tenant_id,
                        period=period,
                        hold_id=hold_id,
                        event_type="RECLAIM",
                        reserved_delta_microusd=-int(amount),
                        settled_delta_microusd=0,
                        run_id=hold_id,
                        run_id_is_fallback=True,
                        settle_reason="reaper_reclaim",
                        actor="reaper",
                        source=str(hold.get("source") or "inline"),
                        # The Delete below destroys the hold row, so what this
                        # reclaim was ABOUT survives only here.
                        reaped_hold_facts=_reaped_hold_facts(hold),
                    )
                )
            client.transact_write_items(
                TransactItems=_reaper_items,
                ClientRequestToken=_fresh_idempotency_token(),
            )
            reclaimed += 1
            # PENDING protocol (PR-1): the reclaim above returned this ACTIVE hold's
            # headroom atomically; settle its separate marker as cleanup (best-effort,
            # money-neutral — an EXPIRED hold is never EXPIRED_UNCREDITED).
            if _reserve_protocol_for(tenant_id) == "pending" and hold_id:
                budgets.marker_settle_best_effort(tenant_id=tenant_id, hold_id=hold_id)
            # C3.5: this reclaim just returned the reservation and recorded a
            # settled delta of ZERO, which is the right record only if nothing was
            # ever charged for it. A settle that observed usage and then failed to
            # commit leaves an OWED_SETTLE row saying otherwise, and this is where
            # that row is honoured — through the same LATE_SETTLE recovery the
            # settle path uses when the reaper beats it, which is conditional on the
            # terminal being the RECLAIM we just wrote and once-per-hold on its own
            # sk. So a re-drive cannot double-post, and a reclaim of a hold with no
            # owed row costs one point read.
            if hold_id:
                _recover_owed_settle_after_reclaim(
                    client=client, budgets=budgets, tenant_id=tenant_id,
                    period=period, hold_id=hold_id)
            # error level, with amount: an orphan means a request that reserved
            # budget then vanished. If the crash was AFTER a successful Bedrock
            # call, real spend happened but is recorded here as actual=0 — this
            # line + pool_reclaimed_microusd let operators reconcile the bill.
            #
            # `exposure_microusd` is that amount when, and only when, the hold
            # backed a provider call (`source == "inline"`). It is the money this
            # reclaim hands back on a request that may well have been billed —
            # what a retained-liability design would keep held instead. An
            # external authorization hold made no provider call, so returning it
            # is simply correct and it is not exposure.
            _source = str(hold.get("source") or "")
            logger.error(
                "pool_hold_reclaimed",
                tenant_id=tenant_id,
                period=period,
                hold_id=hold_id,
                amount_microusd=amount,
                hold_source=_source or "unknown",
                exposure_microusd=amount if _source == "inline" else 0,
                provider_invoked_at=hold.get("provider_invoked_at") or "",
            )
        except ClientError as e:
            # A cancelled transaction means the hold was already reclaimed or
            # settled by a concurrent path — expected under contention, not an
            # error. Anything else is transient; the next request sweeps again.
            code = e.response.get("Error", {}).get("Code", "")
            if code != "TransactionCanceledException":
                logger.warning(
                    "pool_hold_reclaim_failed",
                    tenant_id=tenant_id,
                    period=period,
                    sk=sk,
                    error_code=code,
                )
        except Exception:  # noqa: BLE001 — never let the reaper fail the request
            logger.warning(
                "pool_hold_reclaim_failed",
                tenant_id=tenant_id,
                period=period,
                sk=sk,
                error_code="non_client_error",
            )
    return reclaimed


# ---------------------------------------------------------------------------
# PENDING protocol background jobs (docs/design/pending-protocol.md). Neither is
# on the hot path; a scheduled worker calls them. Kept here so they share the
# repositories and the ledger helpers the reserve/reap paths use.
# ---------------------------------------------------------------------------

def sweep_fence_pending(budgets, tenant_id: str, period: str, *, cap: int = 50) -> int:
    """Sweeper fence (step-3 companion): move EXPIRED, still-PENDING holds to
    EXPIRED_UNCREDITED WITHOUT touching the pool. The sweeper cannot know whether
    a PENDING hold's debit committed (no hold_id capability), so it never credits
    back — a debited-but-fenced hold leaks (bounded) until `reconcile_pool`
    recovers it in aggregate; crediting here would oversell an un-debited hold
    (I1'). Returns the count fenced. Best-effort, never raises."""
    now_epoch = int(time.time())
    fenced = 0
    try:
        pending = budgets.query_pending_expired_holds(
            tenant_id=tenant_id, period=period, now_epoch=now_epoch, limit=cap)
    except Exception:  # noqa: BLE001
        return 0
    for hold in pending:
        sk = str(hold.get("sk", ""))
        if not sk:
            continue
        try:
            if budgets.fence_pending_expired(tenant_id=tenant_id, sk=sk):
                fenced += 1
                logger.info(
                    "pending_hold_fenced", tenant_id=tenant_id, period=period,
                    hold_id=str(hold.get("hold_id", "")),
                    amount_microusd=int(hold.get("amount_microusd", 0)),
                )
        except Exception:  # noqa: BLE001 — a fence failure just defers to next sweep
            continue
    return fenced


def reconcile_pool(budgets, tenant_id: str, period: str) -> dict:
    """Marker-driven leak recovery (docs/design/pending-protocol.md, Fable marker
    design). NEVER an admission authority — authority is always the step-2
    conditional headroom check; this recovers debited-but-orphaned reservations.

    With the per-hold `applied` marker, recovery is DETERMINISTIC and PER-HOLD, not
    an aggregate-drift guess (which had a livelock on hot pools — Fable review bug
    5b). For each EXPIRED_UNCREDITED hold, `pool_credit_back` credits it back iff
    its marker still exists — an ATOMIC remove-marker + add-headroom, so double
    credit is structurally impossible and a hold whose debit never committed (no
    marker) is skipped (leak-safe, never oversell). Re-run continuously: a
    late-arriving ambiguous debit that lands AFTER a fence is picked up on the next
    pass (the marker appears, then this credits it). Returns a summary for
    logging/alarms. The `available + Σ(applied) == limit - settled` item-local
    invariant is what the canary A1 checker asserts."""
    holds = budgets.list_holds(tenant_id=tenant_id, period=period)
    recovered = 0
    recovered_count = 0
    still_uncredited = 0        # retire_reclaimed_best_effort failures (hold rescanned next pass)
    credit_back_deferred = 0    # credit-back skipped this pass (transient/invariant), NOT retired
    for h in holds:
        if str(h.get("status", "")) != "EXPIRED_UNCREDITED":
            continue
        hold_id = str(h.get("hold_id", ""))
        if not hold_id:
            continue
        # Credit back iff the marker is still present + RESERVED (debit committed,
        # not yet returned). Transactional phase CAS; a second pass finds it SETTLED
        # and returns False (no double credit). pool_credit_back now RAISES on a
        # TRANSIENT cancel (pool row missing / TransactionConflict / throttle —
        # nothing committed), distinct from the definitive False (marker absent or
        # already SETTLED). On a transient we must NOT retire the hold — leave it
        # EXPIRED_UNCREDITED so the NEXT reconcile pass retries the credit (Fable
        # PR-1 review Bug 2: retiring here would strand a still-debited hold =
        # permanent leak).
        try:
            credited = budgets.pool_credit_back(
                tenant_id=tenant_id, period=period, hold_id=hold_id)
        except ValueError:
            # A NON-transient defect (e.g. marker/period mismatch) — retrying every
            # pass would make this a silent poison item. Alarm it and skip WITHOUT
            # retiring (leaving the hold visible for a human), never crediting.
            logger.error("pool_reconcile_credit_back_invariant", tenant_id=tenant_id,
                         period=period, hold_id=hold_id)
            credit_back_deferred += 1
            continue
        except Exception:  # noqa: BLE001 — transient credit-back failure: retry next pass
            logger.warning("pool_reconcile_credit_back_transient", tenant_id=tenant_id,
                           period=period, hold_id=hold_id)
            credit_back_deferred += 1
            continue   # do NOT retire; the hold stays EXPIRED_UNCREDITED for retry
        if credited:
            recovered += int(h.get("amount_microusd", 0))
            recovered_count += 1
        # Retire the hold row so it stops being rescanned (starvation fix). Reached
        # only when credit-back was DEFINITIVE (credited, or False = no/settled
        # marker): whether or not a marker existed, this EXPIRED_UNCREDITED hold is
        # now fully accounted for and safe to retire.
        try:
            budgets.retire_reclaimed_best_effort(tenant_id=tenant_id, sk=str(h.get("sk", "")))
        except Exception:  # noqa: BLE001
            still_uncredited += 1
    # AUDIT SWEEP (Fable PR-1 Q2 hole 3 + review Bug 1): a settle/release/reclaim
    # returns headroom + deletes the hold atomically, then settles the SEPARATE
    # marker best-effort. If that best-effort transition is lost (crash between the
    # two), a RESERVED marker is stranded with NO owning hold row. That is a STORAGE
    # orphan, not a money leak (headroom was already returned by the terminal txn),
    # but a stranded RESERVED marker would (a) look outstanding and (b) never become
    # TTL-eligible. Settle ONLY such orphans, NEVER crediting the pool.
    #
    # CRITICAL (review Bug 1): a marker must be settled here ONLY when NO hold row of
    # ANY status still exists for it. Keying the "is it live?" test on the
    # EXPIRED_UNCREDITED loop's snapshot was a permanent-leak bug: a PENDING/ACTIVE
    # hold's RESERVED marker would be wrongly SETTLED, and when that hold later
    # became EXPIRED_UNCREDITED the credit-back phase CAS would fail forever
    # (debited headroom never recovered). So we (1) build the set of ALL present
    # hold_ids (list_holds returns every status), and (2) additionally require the
    # marker to be OLDER than the maximum hold lifetime + margin, so no in-flight
    # hold whose row is merely lagging our read could still claim it.
    stale_markers_settled = 0
    present_hold_ids = {str(h.get("hold_id", "")) for h in holds}
    orphan_min_age = _AUTHORIZE_MAX_TTL_SECONDS + _MARKER_ORPHAN_MARGIN_SECONDS
    now_epoch = int(time.time())
    try:
        for m in budgets.list_reserved_markers(tenant_id=tenant_id):
            m_hold_id = str(m.get("hold_id", ""))
            if not m_hold_id:
                continue
            # PERIOD SCOPE (Fable PR-1 final review, cross-period leak): markers are
            # keyed MARKER#<hold_id> (NOT period-scoped in the SK), so this scan
            # returns EVERY period's markers — but `present_hold_ids` and
            # `hold_exists_by_id` below only see THIS reconcile's period. A prior
            # period's still-live EXPIRED_UNCREDITED hold would therefore look
            # orphaned here and be wrongly SETTLED, killing its credit-back when
            # reconcile(prev_period) runs = permanent leak. Only act on markers of
            # THIS period; each period's own reconcile pass handles its markers
            # (the reaper already sweeps previous_period too). Missing period ⇒
            # fail-closed skip.
            m_period = m.get("period")
            if m_period is None or str(m_period) != period:
                continue
            if m_hold_id in present_hold_ids:
                continue   # a hold row (any status) still owns this marker — leave it
            created = _marker_created_epoch(m)
            # FAIL-CLOSED (Fable review condition 1): an unparseable/absent
            # created_at means we CANNOT prove the marker is old enough to be a
            # genuine post-terminal orphan — skip it rather than risk settling a
            # live hold's marker (which would kill its later credit-back). The
            # reserve path always stamps created_at, so this only guards hand-
            # written / legacy rows.
            if created is None or now_epoch - created < orphan_min_age:
                continue   # too young / unknown age — an in-flight hold may lag
            # Robust existence check (Fable review condition 2): do NOT trust the
            # list_holds snapshot's completeness — directly confirm NO hold row of
            # any status exists for this hold_id (strongly-consistent, cold path).
            if budgets.hold_exists_by_id(
                    tenant_id=tenant_id, period=period, hold_id=m_hold_id):
                continue
            budgets.marker_settle_best_effort(tenant_id=tenant_id, hold_id=m_hold_id)
            stale_markers_settled += 1
    except Exception:  # noqa: BLE001 — the audit sweep is best-effort observability
        pass
    # POOL ITEM SIZE GAUGE (Fable next-step review A′): the whole point of the
    # separate-item marker is that the pool item stays SMALL and FLAT. Emit its
    # estimated size here (cold path — one GetItem per reconcile, not the hot path)
    # so an alarm fires the instant a code regression reintroduces per-hold growth
    # on the hot item. This is the live detector that replaces the (now-redundant)
    # 2×-provisioned c=1×3000 flatness re-benchmark: a fixed-size item cannot grow
    # by the WCU∝size argument, and this catches an implementation regression that
    # the deductive argument assumes away. Best-effort; never fails reconcile.
    pool_item_bytes = None
    try:
        pool_item_bytes = budgets.pool_item_size_bytes(tenant_id, period)
        if pool_item_bytes is not None:
            logger.info("pool_item_size", tenant_id=tenant_id, period=period,
                        size_bytes=pool_item_bytes)
    except Exception:  # noqa: BLE001 — the gauge is observability, never money
        pass
    if recovered or stale_markers_settled:
        logger.warning("pool_reconcile_recovered", tenant_id=tenant_id, period=period,
                       recovered_microusd=recovered, holds=recovered_count,
                       stale_markers_settled=stale_markers_settled)
    return {"recovered_microusd": recovered, "recovered_holds": recovered_count,
            # Split (Fable PR-1 follow-up D): distinguish "retire failed" from
            # "credit-back deferred to next pass" — they had been conflated.
            "retire_failures": still_uncredited,
            "credit_back_deferred": credit_back_deferred,
            "stale_markers_settled": stale_markers_settled,
            "pool_item_size_bytes": pool_item_bytes,
            "reason": "recovered" if recovered else "clean"}


def _err_402(reason: str) -> HTTPException:
    # A-08-credit: never leak precise balances/limits to the caller; surface
    # only the machine-readable reason and a generic message. The exhaustion
    # is recorded server-side for operators.
    return HTTPException(
        status_code=402,
        detail={
            "type": "credit_exhausted",
            "reason": reason,
            "message": (
                "Insufficient budget for this request. Contact your admin."
            ),
        },
    )


def _err_402_does_not_fit(reason: str) -> HTTPException:
    """402 for a request whose reservation bound EXCEEDS THE WHOLE `pool_limit`
    (docs/design/hard-ceiling.md item 2b, "cannot fit at all") — exact, no
    configured fraction involved: no amount of waiting or draining the pool
    makes this request admissible to this budget.

    Deliberately a DIFFERENT `reason` than `_err_402("tenant_pool_exhausted")`,
    even though both are HTTP 402 `credit_exhausted`: the two need different
    operator actions. "tenant_pool_exhausted" means wait or top up — the budget
    is fine, it is just busy right now. "request_does_not_fit_pool_limit" means
    THIS request cannot fit THIS budget no matter how quiet the pool is — the
    fix is resizing the pool or shrinking the request, and reporting it as
    ordinary exhaustion would send an operator chasing a transient-capacity
    problem that does not exist.
    """
    return HTTPException(
        status_code=402,
        detail={
            "type": "credit_exhausted",
            "reason": reason,
            "message": (
                "This request's reservation exceeds the tenant's entire "
                "budget for the period; no amount of available headroom "
                "would admit it. Reduce the request size or ask your admin "
                "to raise the budget."
            ),
        },
    )


def _err_400(reason: str) -> HTTPException:
    """400 for a malformed/unservable request input (e.g. an invalid VSR pin)."""
    return HTTPException(
        status_code=400,
        detail={"type": "invalid_request", "reason": reason},
    )


def _err_403(reason: str) -> HTTPException:
    """403 for an authorization failure (e.g. a VSR pin outside the allowlist)."""
    return HTTPException(
        status_code=403,
        detail={"type": "forbidden", "reason": reason},
    )


def _err_503(reason: str) -> HTTPException:
    """503 for a routing input the gateway could not read.

    Same `type` as the contended-reservation 503s so a client's retry logic keys
    on one shape: the request was not refused on its merits and retrying is the
    right response. The `reason` distinguishes which input was missing."""
    return HTTPException(
        status_code=503,
        detail={
            "type": "budget_unavailable",
            "reason": reason,
            "message": (
                "Routing policy is temporarily unavailable. Retry shortly."
            ),
        },
    )


class QuotaExhausted(Exception):
    """A per-model quota condition failed during reserve — the caller's
    cascading fallback should try the next model. Carries which model's quota
    was exhausted so the caller can advance the chain. NOT an HTTP error: it is
    caught by `reserve_with_model_cascade` and only surfaces as 402 if EVERY
    candidate is exhausted.
    """

    def __init__(self, model: str):
        super().__init__(f"quota exhausted for model {model}")
        self.model = model


class ExternalHoldReclaimed(Exception):
    """An external-authorize capture lost the terminal race to the reaper's
    RECLAIM. The hold's reserved was already returned to the pool; per the
    external-capture contract (Fable authcap D-2) we do NOT late-settle it —
    the capture endpoint maps this to HTTP 410 (expired). The counters are
    untouched (the settle txn cancelled), so no spend and no leak."""

    def __init__(self, hold_id: str):
        super().__init__(f"external hold {hold_id} was reclaimed before capture")
        self.hold_id = hold_id


class ExternalHoldInconsistent(Exception):
    """An external hold's two durable amount sources disagree (HOLD row amount vs
    RESERVE event reserved_delta) — a repair/adjust/corruption edited only one.
    Settling would break ledger-derivability (I2), so we refuse and surface it
    (the capture endpoint maps this to 409/500) rather than move money on an
    inconsistent hold (Fable authcap review-4 H-A)."""

    def __init__(self, hold_id: str):
        super().__init__(f"external hold {hold_id} has inconsistent amounts")
        self.hold_id = hold_id


def reserve_credit_for_model(
    user,
    reservation_tokens: int,
    *,
    model_name: str,
    input_tokens_est: int,
    max_output_tokens: int,
    effort_multiplier: int = 1,
    breaker_max_tier: Optional[int] = None,
    wire_protocol: Optional[str] = None,
    vsr_hard_model: Optional[str] = None,
    workflow_run_id: Optional[str] = None,
    group_id: Optional[str] = None,
    request_id: Optional[str] = None,
    saar_prefer_model: Optional[str] = None,
    vsr_decision: Optional[dict] = None,
    input_bytes: Optional[int] = None,
    payload_hash: Optional[str] = None,
    extra_input_tokens: int = 0,
    shadow_mode: bool = False,
) -> ReservationContext:
    """Reserve credit for a request, with per-model quota + cascading fallback.

    **Hard-ceiling reservation bound** (docs/design/hard-ceiling.md). `input_bytes`
    is the UTF-8 byte count of the CANONICAL outbound payload's non-image
    content (section 3a: the payload the gateway is actually about to send to
    Bedrock, NOT the raw request body — see the route handler for how that is
    built before this is called; section 3b: this deliberately EXCLUDES image
    payload bytes, which are covered by `extra_input_tokens` instead, so
    counting both would double-charge) — a BOUND on the input token count, not
    an estimate. `payload_hash` is a hash of the ENTIRE canonical payload
    INCLUDING image bytes (a byte-length-only pin would let a retry swap an
    image while keeping the length), pinned onto the hold alongside the byte
    count (section 3a) so a retry can be verified byte-identical rather than
    merely trusted to be. When `input_bytes` is supplied, the sound bound is
    ALWAYS computed (and recorded — see `shadow_mode` below) by
    `mvp.reservation_bound` instead of the legacy `estimate_cost_microusd`, in
    the mode the tenant's `bound_mode` resolves to (`strict` — sound by
    construction, within section 4's stated assumptions — or `calibrated`, out
    of scope for this change — see `dynamo.tenants.resolve_bound_mode`).
    `extra_input_tokens` is the bounded image-dimension token term
    `mvp.reservation_bound.assess_boundability` computed.

    **`shadow_mode`** (section 9b's rollout requirement) is the ONE additional
    fork `input_bytes is not None` needs, and it answers a DIFFERENT question
    than "is the bound computed": whether the bound may also be what gets
    RESERVED (against the pool and, incidentally, any per-model quota).
    `shadow_mode=False` (the default — matches every caller's behaviour before
    shadow mode existed, and the `enforced`/`measured` states) reserves the
    bound itself, exactly as before. `shadow_mode=True` (the `shadow` state —
    a dollar pool row exists but
    `mvp.reservation_bound.dollar_pool_bound_should_gate` is False) reserves
    the LEGACY `estimate_cost_microusd` amount instead — byte-for-byte the same
    number this function would have reserved had the hard-ceiling bound never
    shipped — while the sound bound is still computed and lands on
    `ReservationContext.measured_bound_microusd` regardless, because comparing
    the bound against what settle actually charges is the entire reason
    `shadow` exists (section 9b: "compute and record the bound first, measure
    what it would refuse on real traffic, ... only then let it gate
    admission"). The CALLER — a route, which already calls
    `dollar_pool_bound_should_gate` to decide whether to enforce
    `assess_boundability`'s refusal — is the one place that knows which state
    a request is in; this parameter is threaded straight from that decision
    rather than re-derived here, so the refusal check and the reservation
    amount can never disagree about which state governs a given request.

    **Budget enforcement is opt-in** (section 7a/7b): the CALLER decides
    whether to compute a byte count at all — see
    `mvp.reservation_bound.dollar_pool_bound_enforcement_active`, which the
    route handler consults BEFORE surveying the payload, so a tenant with no
    dollar pool never pays for that survey and never passes `input_bytes`
    here. An unreadable image, in particular, is refused by the ROUTE (before
    ever calling this function) when — and only when — enforcement is
    active; this chokepoint never independently re-derives that refusal.

    `input_bytes=None` (the default) keeps the exact legacy behaviour, for any
    caller not yet migrated to compute a byte count (or for a tenant this
    request's caller determined has no active dollar-pool enforcement).

    The single chokepoint every route handler calls. Two regimes:

    - **No routing config** (the common case): prices the pool debit from the
      requested model and delegates to `reserve_credit` — exactly the per-user
      token reservation as before, plus the pool debit when a pool is present.
      `context.selected_model` is the requested model so the handler invokes it.

    - **Routing config present** (P0-11): resolves the ordered candidate chain
      (allowlist ∩ chain ∩ breaker tier, honouring the fallback toggle) and
      tries each candidate in turn. Each candidate is priced from ITS OWN
      `pricing_key` — a cheaper fallback is reserved AND later settled at the
      cheaper rate — and reserved against its per-model quota inside the same
      atomic transaction as the pool debit. A `QuotaExhausted` on one candidate
      advances to the next; a budget 402 (no money at all) surfaces immediately.
      If every candidate's quota is exhausted, 402 `model_quota_exhausted`.

    The chosen model is returned as `context.selected_model`; the handler
    re-resolves that to a Bedrock model id for the actual invoke.

    **VSR hard pin (P0-15).** When `vsr_hard_model` is set, the request is pinned
    to exactly that model: no cascade, no chain rewrite, no breaker downgrade, no
    quota-exhaustion fallback. The pin is validated first — it must resolve in
    the registry and (when `wire_protocol` is given) speak this route's protocol
    (else `_err_400("invalid_model_pin")`), and it must be in the tenant allowlist
    when one is configured (else `_err_403("model_pin_not_allowed")`). A pinned
    model whose quota is exhausted 402s (`model_quota_exhausted`) rather than
    falling back — that is what "hard" means. Pricing/quota apply at the pinned
    model's own rate, exactly as if the cascade had landed on it.
    """
    from .models import resolve_model as _resolve_pricing
    from .pricing import estimate_cost_microusd
    from .routing.config import (
        RoutingConfigUnavailable,
        get_tenant_routing_config,
        get_user_routing_config,
    )

    # Resolved ONCE per request, not per cascade candidate: a tenant's bound
    # mode is one fact, and switching bounding strategy mid-cascade would make
    # "which candidate we landed on" also decide "how sound the price is",
    # which is not a decision the cascade should be making. Only consulted (and
    # only costs a Dynamo read) when the caller actually supplied a byte count
    # — a request still on the legacy path never pays for this lookup.
    # docs/design/hard-ceiling.md section 7b: the caller (the route handler) is
    # the one place that knows whether enforcement is worth checking BEFORE
    # paying for the survey that produces `input_bytes` — see
    # `mvp.reservation_bound.dollar_pool_bound_enforcement_active`, which the
    # route consults first. So `input_bytes is not None` HERE already means
    # "enforcement is active for this tenant"; this chokepoint does not
    # re-derive that decision (a second existence read would be redundant on
    # every path that reaches here, since the real reserve transaction below
    # performs its own strongly-consistent read regardless). A caller that
    # skipped the gate and passed `input_bytes` for an unenforced tenant
    # anyway would simply price a bound that never gets enforced against
    # (harmless — the pool-existent branch below is what actually debits
    # anything, and it still requires a real pool row).
    _bound_mode: Optional[str] = None
    if input_bytes is not None:
        from dynamo.tenants import resolve_bound_mode

        _bound_mode = resolve_bound_mode(user.org_id)

    def _freeze(pk: str) -> "RateSnapshot":
        """The rate this request is admitted at, frozen before anything is priced.

        Every amount this function's callers compute — the legacy estimate, the
        sound bound, the `shadow_mode` amount — is priced from the object returned
        here, and the same object travels to the settle. Pricing the admission from
        a live read and freezing a second one afterwards left a window in which the
        two disagreed, and the failure of the freeze was then absorbed by charging
        at whatever the table said at settle time. A rate the gateway cannot freeze
        is a request it cannot price: fail closed, before the provider is called.
        """
        from .pricing import snapshot_rates

        try:
            return snapshot_rates(pk)
        except Exception as e:  # noqa: BLE001 — classified into one refusal below.
            logger.error("RateSnapshotFailed", pricing_key=pk, error=str(e))
            raise _err_503("pricing_unavailable") from None

    def _legacy_estimate(snap: "RateSnapshot") -> int:
        # Factored out of the `input_bytes is None` branch below so
        # `shadow_mode` can compute this EXACT same number for the reservation
        # while the hard-ceiling branch, a few lines down, still computes and
        # returns the sound bound for recording — see `_price`'s own
        # docstring.
        #
        # This used to discount the warm model's input leg by SAAR's cache
        # evidence, so that a switch which would breach the pool was gated at the
        # 402 while a stay fitted. The discount is gone: the provider decides at
        # settle which leg each token bills at, so reserving the cheaper leg
        # reserves below the charge. The warm preference is expressed by candidate
        # ORDER (`vsr_hard_model`, `saar_prefer_model`), which is where it was
        # always doing the real work, and the money claim about staying warm is
        # `switch_cost_delta_microusd` on the decision record.
        #
        # Priced from the FROZEN snapshot, not from a live read: this number gates
        # admission, and the settle charges from `snap`, so the two must be one
        # document. See `_freeze`.
        from .pricing import estimate_cost_from_rates

        return estimate_cost_from_rates(
            snap,
            input_tokens_est=input_tokens_est,
            max_output_tokens=max_output_tokens,
            effort_multiplier=effort_multiplier,
        )

    def _price(
        model: str,
    ) -> tuple[str, int, Optional["RateSnapshot"], Optional[int]]:
        """`(pricing_key, reserved_cost_microusd, rate_snapshot, bound_microusd)`.

        `bound_microusd` is the sound bound `mvp.reservation_bound` computed
        for this candidate, independently of what actually gets reserved —
        None on the legacy/`accounting` path (`input_bytes is None`, no bound
        exists at all), populated on every hard-ceiling path (`measured`,
        `shadow`, `enforced`) even when `shadow_mode` means it is NOT what
        gets reserved. `reserved_cost_microusd` is what actually gates
        admission (the pool debit, and any per-model quota amount): the bound
        itself, UNLESS `shadow_mode` says this pool has not yet earned the
        right to enforce it, in which case it is `_legacy_estimate` instead —
        see `_price`'s enclosing function's docstring for why reserving the
        smaller legacy number while still recording the bound is the entire
        point of `shadow_mode`.
        """
        try:
            pk = _resolve_pricing(model).pricing_key
        except ValueError:
            pk = "default"
        if input_bytes is None:
            # Legacy path for a caller not yet migrated to supply a byte count: no
            # bound exists to record (None) — this is the `accounting` state, where
            # nothing is bounded at all. The RATE, though, is frozen here like
            # everywhere else and the estimate is priced from it, so this path can
            # no longer size a reservation at one document and charge it at
            # another (it used to leave `snap=None` and let `reserve_credit` freeze
            # a second, possibly different one).
            snap = _freeze(pk)
            return pk, _legacy_estimate(snap), snap, None
        # Hard-ceiling path (docs/design/hard-ceiling.md item 1). Deliberately no
        # warm/cold split here — a SAAR "this will hit the cache" expectation
        # is exactly the kind of provider-behaviour assumption a SOUND bound
        # must not make (assuming a cache outcome instead of bounding against
        # every one is the original cache-write defect's shape). A calibrated
        # tenant still gets no warm discount either: the calibration is a
        # measured tokens-per-byte ratio, not a caching assumption, and mixing
        # the two would make a settle's realised-ratio signal ambiguous about
        # which effect moved it. `extra_input_tokens` (the image-dimension
        # bound) is additive input-side tokens, same as the byte-derived count.
        #
        # Item 5b ("a rate change between reserve and settle"): the bound is
        # priced from a FROZEN snapshot (`snapshot_rates`, the SAME Layer-5
        # primitive that pins the settle-time charge), not a second, separate
        # live-table read. Returning that snapshot so `reserve_credit` uses it
        # directly (rather than freezing its own) is what makes "settle at the
        # reserve-time rates" airtight for this path: the bound and the charge
        # then price at the IDENTICAL row by construction, so a rate document
        # edit that lands between pricing this candidate and committing the
        # reserve transaction cannot make settle charge above what was bounded
        # — the edit is simply not visible to either side of this request.
        from .reservation_bound import (
            calibrated_reservation_microusd,
            calibration_store,
            strict_reservation_microusd,
        )
        from .rates import Rate as _Rate
        from dynamo.tenants import BOUND_MODE_CALIBRATED

        # `_freeze` refuses the request when the rate cannot be frozen. There used
        # to be a degraded branch here that priced the bound from a live read and
        # left `snap=None`, so the reservation was labelled `snapshot-failed` and
        # the settle charged from whatever the table said later — a rate edit
        # between admission and settle then changed what this request was charged,
        # which is the one thing the frozen rate exists to prevent.
        snap = _freeze(pk)
        rate = _Rate(
            input_per_mtok_microusd=snap.input_per_mtok_microusd,
            output_per_mtok_microusd=snap.output_per_mtok_microusd,
            cache_read_per_mtok_microusd=snap.cache_read_per_mtok_microusd,
            cache_write_per_mtok_microusd=snap.cache_write_per_mtok_microusd,
        )
        if _bound_mode == BOUND_MODE_CALIBRATED:
            bound_cost = calibrated_reservation_microusd(
                rate,
                input_bytes=input_bytes,
                max_output_tokens=max_output_tokens,
                effort_multiplier=effort_multiplier,
                extra_input_tokens=extra_input_tokens,
                tokens_per_byte=calibration_store.get(pk),
            )
        else:
            bound_cost = strict_reservation_microusd(
                rate,
                input_bytes=input_bytes,
                max_output_tokens=max_output_tokens,
                effort_multiplier=effort_multiplier,
                extra_input_tokens=extra_input_tokens,
            )
        # docs/design/hard-ceiling.md section 9b: the bound is ALWAYS computed
        # and returned once we are even in this branch (`should_compute` is
        # why `input_bytes` is not None at all) — what `shadow_mode` decides
        # is whether it is ALSO what gets reserved. `shadow_mode` is the
        # caller's already-made decision (it already consulted
        # `dollar_pool_bound_should_gate` for the refusal check above this
        # call), threaded straight through rather than re-derived here, so
        # this chokepoint and the route's refusal check can never disagree
        # about which state a request is in. In `shadow_mode` the reserved AMOUNT
        # is the legacy estimate rather than the bound — a different STRATEGY, on
        # purpose — but it is priced from the SAME frozen snapshot the bound and
        # the settle use, so strategy is the only thing that differs. It used to
        # be priced from a live read as well, which made the reservation and the
        # charge disagree about the rate on top of the token count.
        reserved_cost = _legacy_estimate(snap) if shadow_mode else bound_cost
        return pk, reserved_cost, snap, bound_cost

    # A routing config that could not be read is not a config: its quotas and its
    # allowlist only ever restrict this request, so proceeding without it would
    # admit exactly what the tenant configured against. The loader serves a
    # last-known-good value when it has one and raises otherwise; fail closed on a
    # retryable 503 rather than route unrestricted.
    try:
        tenant_cfg = get_tenant_routing_config(user.org_id)
    except RoutingConfigUnavailable:
        logger.warning("routing_config_unavailable_fail_closed", tenant_id=user.org_id)
        raise _err_503("routing_config_unavailable") from None

    # VSR hard pin (P0-15): validate, then force the candidate list to exactly
    # [pin] and fall through to the same reserve loop (pricing + quota + atomic
    # reserve) — so pinning reuses all the money machinery, only the candidate
    # SELECTION changes. Handled before the no-config passthrough so a pin is
    # honoured whether or not the tenant has routing config.
    def _stamp_bound_metadata(ctx: ReservationContext) -> None:
        # docs/design/hard-ceiling.md item 4: carry the exact inputs `cost` was
        # computed from onto the context, so settle can embed a RECOMPUTABLE
        # reservation on the ledger terminal instead of an opaque amount.
        # `bound_mode` stays None (the dataclass default) on the legacy path
        # (input_bytes is None) — that is itself the signal that this
        # reservation was priced by the old heuristic, not a bound.
        #
        # `shadow_mode` is the SAME signal for a different reason: `_price`
        # priced THIS reservation's `cost_microusd` (what `pool_reserved_microusd`
        # ends up holding) from `_legacy_estimate`, not the bound, even though
        # `input_bytes` was supplied and the bound WAS computed (it lands on
        # `measured_bound_microusd` instead — see `reserve_credit`'s
        # `bound_microusd`). Stamping `bound_mode="strict"` here anyway would
        # tell a reader of the ledger terminal "recompute `reserved_microusd`
        # via `strict_reservation_microusd` from these inputs" — which is
        # false in `shadow_mode`; the honest recomputation for what actually
        # got reserved is `estimate_cost_microusd`, exactly the legacy
        # convention `bound_mode is None` already means. Leaving these fields
        # at the dataclass default in `shadow_mode` also has the side effect
        # of skipping the settle-time `reservation_bound_overrun` alarm for a
        # reservation that was never bound-priced to begin with — correct,
        # since that alarm's whole premise ("a sound bound is never exceeded")
        # was never asserted for this request's actual reservation.
        if input_bytes is not None and not shadow_mode:
            ctx.bound_mode = _bound_mode
            ctx.reserved_input_bytes = int(input_bytes)
            ctx.reserved_payload_hash = payload_hash
            ctx.reserved_extra_input_tokens = int(extra_input_tokens)
            ctx.reserved_max_output_tokens = int(max_output_tokens)
            ctx.reserved_effort_multiplier = int(effort_multiplier)

    def _stamp_requested(ctx: ReservationContext) -> ReservationContext:
        # Record the client-requested model (pre-cascade) on the context so
        # settle can log P0-11 fallback visibility. Single chokepoint => every
        # handler gets it without threading through each reserve return path.
        _stamp_bound_metadata(ctx)
        ctx.requested_model = model_name
        # Same chokepoint stamps the request attribution so settle keys the
        # ledger run-index on the client's workflow_run_id (per-run billing).
        ctx.workflow_run_id = workflow_run_id
        ctx.group_id = group_id
        ctx.request_id = request_id
        # Carry the VSR consult decision (observability only) so the decision
        # record can be joined to the committed/billed model by span_id.
        ctx.vsr_decision = vsr_decision
        # Complete the decision facts with the estimate inputs the candidates
        # were priced against (P0 decision log), then fire-and-forget the
        # reserve-time decision record. The WHOLE block is fenced: this runs after
        # the reserve committed, so int(None)/any error must not fail the request
        # (Fable RDL review High). Attribution is best-effort. A VSR decision
        # alone (no routing facts, single-candidate passthrough) still emits the
        # record — record_decision_from_context handles the facts-absent case.
        if ctx.decision_facts is not None:
            try:
                ctx.decision_facts["estimate_inputs"] = {
                    "input_est": int(input_tokens_est),
                    "max_out": int(max_output_tokens),
                    "effort": int(effort_multiplier),
                }
            except Exception:  # noqa: BLE001 — never fail reserve on attribution.
                pass
        if ctx.decision_facts is not None or vsr_decision:
            try:
                from .learning.decision_log import record_decision_from_context
                record_decision_from_context(ctx)
            except Exception:  # noqa: BLE001 — decision logging never breaks reserve.
                pass
        return ctx

    if vsr_hard_model:
        _validate_model_pin(vsr_hard_model, tenant_cfg, wire_protocol)
        ctx = _reserve_over_candidates(
            user, reservation_tokens, candidates=[vsr_hard_model],
            tenant_cfg=tenant_cfg, price=_price,
            payload_hash=payload_hash, payload_bytes=input_bytes,
        )
        # A VSR hard pin is a deliberate policy override, NOT a P0-11 quota
        # cascade fallback (Fable #65 rev1 BUG 2). Record the effective (pinned)
        # model as the "requested" one so the pin never inflates fallback_count
        # or shows a spurious fallback badge — the two events are semantically
        # different and the derived bool must not conflate them.
        _stamp_bound_metadata(ctx)
        ctx.requested_model = ctx.selected_model or vsr_hard_model
        ctx.workflow_run_id = workflow_run_id
        ctx.group_id = group_id
        ctx.request_id = request_id
        # Observability: carry + record the VSR decision (fire-and-forget). A
        # hard pin normally has no multi-candidate decision_facts, so this is the
        # only place a hard-applied VSR decision reaches the decision log.
        ctx.vsr_decision = vsr_decision
        if vsr_decision:
            try:
                from .learning.decision_log import record_decision_from_context
                record_decision_from_context(ctx)
            except Exception:  # noqa: BLE001 — decision logging never breaks reserve.
                pass
        return ctx
    # No routing config at all → passthrough on the requested model (fully
    # backward compatible: same reservation as before, no quota lines).
    if not tenant_cfg.chain and not tenant_cfg.allowlist and not tenant_cfg.quotas:
        pk, cost, snap, bound = _price(model_name)
        return _stamp_requested(reserve_credit(
            user, reservation_tokens,
            pricing_key=pk, cost_microusd=cost,
            selected_model=model_name,
            rate_snapshot=snap,
            payload_hash=payload_hash,
            payload_bytes=input_bytes,
            bound_microusd=bound,
        ))

    # Same discipline as the tenant config: a user chain NARROWS the candidate
    # set, so serving the request without it would widen what this user may reach.
    try:
        user_cfg = get_user_routing_config(user.org_id, user.user_id)
    except RoutingConfigUnavailable:
        logger.warning("routing_config_unavailable_fail_closed",
                       tenant_id=user.org_id, user_id=user.user_id)
        raise _err_503("routing_config_unavailable") from None
    candidates = _resolve_candidate_chain(
        requested_model=model_name,
        tenant_cfg=tenant_cfg,
        user_cfg=user_cfg,
        breaker_max_tier=breaker_max_tier,
        wire_protocol=wire_protocol,
    )
    # SAAR soft preference (Fable review-1 C2): move the session's warm model to
    # the HEAD of the already-resolved candidate list so it is tried first (prefix-
    # cache locality), but keep the rest of the chain intact as fallback. This is a
    # pure REORDER of models the cascade already validated (allowlist ∩ chain ∩
    # breaker tier) — it never injects a new model and never disables fallback, so
    # a warm model that is disallowed/quota-exhausted simply isn't in the list and
    # the request still cascades exactly as pre-SAAR (cannot reduce availability).
    if saar_prefer_model and saar_prefer_model in candidates:
        candidates = [saar_prefer_model] + [m for m in candidates if m != saar_prefer_model]
    return _stamp_requested(_reserve_over_candidates(
        user, reservation_tokens, candidates=candidates,
        tenant_cfg=tenant_cfg, price=_price,
        payload_hash=payload_hash, payload_bytes=input_bytes,
    ))


def _reserve_over_candidates(
    user, reservation_tokens, *, candidates, tenant_cfg, price,
    payload_hash=None, payload_bytes=None,
):
    """Walk an ordered candidate list, pricing + quota-reserving each atomically.

    Shared by the P0-11 cascade and the P0-15 hard pin (a pin is just a
    one-element candidate list). QuotaExhausted advances to the next candidate;
    if every candidate's quota is exhausted, 402 `model_quota_exhausted` (for a
    single-element pin list that means: the pinned model's quota is gone, no
    fallback — the hard-pin contract)."""
    from .models import canonical_model_id as _canonical_model_id
    from .routing import quota as _quota

    period = current_period()
    # Price candidates LAZILY inside the loop, exactly as the money path did
    # before the decision log existed: candidate N is only priced when 1..N-1
    # were exhausted. This keeps `price()` failures from affecting reserve
    # availability (Fable RDL review-2 H1 — pricing the whole list up front added
    # a failure mode that didn't exist). `priced_tried` accumulates the
    # actually-tried candidates for the decision facts; the untried tail is priced
    # LATER, inside the best-effort fence.
    priced_tried: list = []  # (model, pricing_key, est_cost) for tried candidates
    exhausted: set[str] = set()
    for idx, model in enumerate(candidates):
        pk, cost, snap, bound = price(model)
        priced_tried.append((model, pk, cost))
        # Look the limit up under the model's CANONICAL spelling. The admin write
        # path stores quota keys canonicalised, while a candidate can be the raw
        # `body.model` (a tenant with quotas but no chain routes the requested
        # model as-is), so a raw-string lookup found nothing whenever the client
        # spelled the model any other way — no quota line, request unmetered.
        # Keying on the model rather than on its spelling closes that, and the
        # counter key (`quota._sk`) canonicalises identically.
        q = tenant_cfg.quotas.get(_canonical_model_id(model))
        tenant_limit = q.limit if q else None
        if q is not None and tenant_limit is not None:
            # The cap must be enforced in the denomination it was WRITTEN in. This
            # loop reserves micro-USD, and nothing used to read `unit`: a row
            # saying `tokens` was enforced as dollars, so the cap in force differed
            # from the configured one by the price per token — while the operator's
            # console still showed their number. The admin write path pins
            # `usd_micro`, so a row in any other unit predates that pin or arrived
            # out of band; refuse rather than enforce a cap nobody configured.
            unit = str(getattr(q, "unit", _quota.RESERVED_UNIT) or "")
            if unit != _quota.RESERVED_UNIT:
                logger.error("quota_unit_unsupported", tenant_id=user.org_id,
                             model=_canonical_model_id(model), unit=unit)
                raise _err_503("quota_unit_unsupported")
        quota_lines = (
            _quota.build_reserve_txn_items(
                tenant_id=user.org_id, user_id=user.user_id, model=model,
                period=period, amount=cost, tenant_limit=tenant_limit,
            )
            if (cost and tenant_limit is not None)
            else None
        )
        try:
            ctx = reserve_credit(
                user, reservation_tokens,
                pricing_key=pk, cost_microusd=cost,
                quota_lines=quota_lines,
                quota_model=model if quota_lines else None,
                selected_model=model,
                rate_snapshot=snap,
                payload_hash=payload_hash,
                payload_bytes=payload_bytes,
                bound_microusd=bound,
            )
            # Decision-facts construction must NEVER fail the reserve: the hold is
            # already committed here, so any exception (incl. pricing the untried
            # tail) would leak it to the reaper. Fence it — attribution is
            # best-effort (Fable RDL review High + review-2 H1).
            try:
                ctx.decision_facts = _build_decision_facts(
                    priced_tried, candidates[idx + 1:], price, exhausted
                )
                if ctx.rate_snapshot is not None:
                    ctx.decision_facts["chosen"]["pricing_version_at_decision"] = (
                        ctx.rate_snapshot.version
                    )
            except Exception:  # noqa: BLE001 — decision log never breaks reserve.
                ctx.decision_facts = None
            return ctx
        except QuotaExhausted as e:
            logger.info("quota_cascade_advance", tenant_id=user.org_id,
                        exhausted_model=e.model, period=period)
            exhausted.add(model)
            continue
    logger.info("model_quota_all_exhausted", tenant_id=user.org_id, period=period)
    raise _err_402("model_quota_exhausted")


def _build_decision_facts(priced_tried, untried_models, price, exhausted) -> dict:
    """Assemble the routing decision facts (P0 decision log).

    `priced_tried` = [(model, pricing_key, est_cost)] for the candidates actually
    tried (the LAST is the chosen one that committed); `untried_models` = the
    servable tail ranked below the chosen (never tried — priced HERE, inside the
    caller's best-effort fence, so a tail pricing failure cannot affect reserve);
    `exhausted` = models whose quota was gone. Tried-but-not-chosen →
    quota-exhausted; untried tail → fallback-order. All are servable (the chain
    was servability-filtered upstream)."""
    chosen_model, chosen_pk, chosen_cost = priced_tried[-1]
    chosen_idx = len(priced_tried) - 1
    # Price the untried tail now (inside the fence).
    #
    # `price()` returns four values — (pricing_key, reserved_cost, rate_snapshot,
    # bound) — while this loop reads three. Splatting it produced a 5-tuple per
    # untried candidate and the unpack below raised, which the caller's fence
    # swallowed: any request whose cascade had an untried tail silently lost its
    # decision record, and the record is what makes a routing saving reproducible.
    # Take the two fields this function actually uses, by name.
    priced = list(priced_tried)
    for m in untried_models:
        # Once per candidate: `price()` reads the rate table and can freeze a
        # snapshot, so calling it twice to pick two fields would be two different
        # reads of a table that can change between them.
        pk, cost = price(m)[:2]
        priced.append((m, pk, cost))
    rejected = []
    for i, (model, pk, cost) in enumerate(priced):
        if i == chosen_idx:
            continue
        reason = "quota-exhausted" if model in exhausted else "fallback-order"
        rejected.append({
            "model": model, "pricing_key": pk, "cost_tier": _tier_or_zero(pk),
            "reject_reason": reason, "servable": True,
            "est_cost_microusd": int(cost),
        })
    return {
        "chosen": {
            "model": chosen_model, "pricing_key": chosen_pk,
            "cost_tier": _tier_or_zero(chosen_pk),
            "est_cost_microusd": int(chosen_cost),
            # The live pricing version at decision time (best-effort; the frozen
            # snapshot on the ctx carries the authoritative one for settle).
            "pricing_version_at_decision": None,
        },
        "rejected": rejected,
    }


def _tier_or_zero(pricing_key: str) -> int:
    try:
        from .routing.chains import _tier_for
        return int(_tier_for(pricing_key))
    except Exception:  # noqa: BLE001 — cost_tier is informational, never critical.
        return 0


def _validate_model_pin(pin: str, tenant_cfg, wire_protocol: Optional[str]) -> None:
    """Validate a VSR hard pin (P0-15). Servability first (400), then policy (403).

    A pin is NOT exempt from these checks — it's a model the route never
    validated, so an unservable or disallowed pin is rejected loudly, never
    silently substituted (the Fable F2/F3/F4 money-bug shape).

    Spelling: the pin is used VERBATIM downstream (candidate list, quota lookup,
    reserve/settle) — deliberately NOT canonicalized. The whole routing config
    (chain/allowlist/quotas) is keyed on raw spellings and P0-11 requires request
    and config to agree on spelling; canonicalizing ONLY the pin (Fable rev1 F1's
    first attempt) steered the quota lookup away from the configured key and
    bypassed the cap (Fable rev2 NEW-1). Treating the pin exactly like the
    requested model keeps one consistent convention.

    Policy boundary (Fable rev2 NEW-2): a pin must sit inside the tenant's
    configured model set. That is the `allowlist` when one exists; for a
    chain-only tenant (no allowlist) the `chain` IS the model policy, so the pin
    must be one of the chain's models — otherwise a client header could escape
    the tenant's routing policy entirely. Only a tenant with neither allowlist
    nor chain (pure passthrough) accepts an arbitrary servable pin."""
    from .models import resolve_model as _resolve_registry

    try:
        entry = _resolve_registry(pin)
    except ValueError:
        raise _err_400("invalid_model_pin")
    if wire_protocol is not None and entry.wire_protocol != wire_protocol:
        raise _err_400("invalid_model_pin")
    if getattr(entry, "served_by", "bedrock") == "vllm":
        # Servability first (400), same as an unservable region: a vLLM pin is
        # only servable with hybrid serving on AND an allowlisted endpoint. Flag
        # off => a vLLM pin is rejected loudly here, never routed with a bogus
        # region into the Bedrock client.
        from .serving.vllm import endpoint_is_servable
        if not endpoint_is_servable(entry.endpoint_key):
            raise _err_400("invalid_model_pin")

    # The pin must be in the tenant's configured model set (allowlist, else
    # chain). Compare on the registry entry so different spellings of the same
    # model match — WITHOUT changing the spelling used downstream.
    policy_set = tenant_cfg.allowlist or tenant_cfg.chain
    if policy_set:
        allowed = False
        for m in policy_set:
            try:
                if _resolve_registry(m) is entry:
                    allowed = True
                    break
            except ValueError:
                if m == pin:
                    allowed = True
                    break
        if not allowed:
            raise _err_403("model_pin_not_allowed")


def _resolve_candidate_chain(
    *,
    requested_model: str,
    tenant_cfg,
    user_cfg,
    breaker_max_tier: Optional[int],
    wire_protocol: Optional[str] = None,
) -> list:
    """Ordered list of models to attempt for a request (P0-11 cascade).

    Mirrors `model_resolver.resolve_model`'s filtering but returns the FULL
    ordered list rather than just the head, so the caller can walk it on quota
    exhaustion. Honours: chain start position, allowlist intersection, breaker
    tier cap, and the tenant/user fallback toggle (which truncates to the head).
    Always returns at least the requested model.

    SERVABILITY FILTER (Fable F2/F3/F4 root fix): the handler invokes whatever
    the cascade selects, so a candidate that can't actually be served on this
    route is a money bug waiting to happen — if it won the cascade, the handler
    would silently invoke the *requested* model instead, PAST its exhausted
    quota and mispriced. So we drop any candidate that (a) doesn't resolve in the
    model registry, or (b) — when `wire_protocol` is given — doesn't speak this
    route's wire protocol. The requested model is exempt from the protocol drop
    (it was already validated by the route) so a bad chain entry never fails an
    otherwise-valid direct request.

    EVERY FILTER HERE NARROWS. An empty admissible set is a refusal, never a
    widening: this function used to fall back to the top of the chain after the
    allowlist filter and to the REQUESTED model after the servability filter, so a
    tenant whose allowlisted models did not all speak this route's protocol ended
    up serving the model the client named — outside the allowlist the operator
    wrote, and with no per-model quota line, because no quota was configured for a
    model the tenant never expected to serve. The pin path already refuses rather
    than substitutes (`_validate_model_pin`); this is the same rule for the
    ordinary path.

    Allowlist membership is decided on the model, not on its spelling: the admin
    write path stores canonical ids, so a client naming the same model by another
    alias is naming an allowed model.
    """
    from .models import canonical_model_id as _canonical_model_id
    from .models import resolve_model as _resolve_registry
    from .routing.model_resolver import _resolve_chain

    _tenant_id = getattr(tenant_cfg, "tenant_id", None)
    fallback_allowed = (tenant_cfg.fallback_default == "on")
    if user_cfg and user_cfg.fallback is not None:
        fallback_allowed = (user_cfg.fallback == "on")

    # `model_resolver.resolve_model` used to be consulted here purely to supply a
    # substitute when a filter emptied the list. There is no substitute any more —
    # an empty admissible set is a refusal — so the head-selection helper is not
    # part of this path; `_resolve_chain` already applies the chain, the user
    # override and the start position.
    candidates = _resolve_chain(requested_model, tenant_cfg, user_cfg)
    if tenant_cfg.allowlist:
        allowed = {_canonical_model_id(m) for m in tenant_cfg.allowlist}
        candidates = [m for m in candidates if _canonical_model_id(m) in allowed]
        if not candidates:
            # Nothing the client can reach on this route is inside the tenant's
            # policy. Refusing is the whole point of an allowlist; substituting a
            # model the client did not ask for, or serving the one it did ask for,
            # both answer a question the operator already answered with "no".
            logger.info("model_not_allowed", tenant_id=_tenant_id,
                        requested_model=requested_model)
            raise _err_403("model_not_allowed")
    if breaker_max_tier is not None:
        # Candidates are model NAMES here, not pricing keys — use the name-aware
        # tier lookup so an alias is resolved through the registry rather than
        # string-matched. A cap that excludes everything is advisory, not policy:
        # the breaker shapes routing, it does not define what the tenant may use,
        # so an empty result keeps the (already policy-filtered) list.
        from .routing.chains import _tier_for_model
        capped = [m for m in candidates if _tier_for_model(m) <= breaker_max_tier]
        candidates = capped or candidates
    if not fallback_allowed:
        candidates = candidates[:1]
    if not candidates:
        logger.info("model_not_allowed", tenant_id=_tenant_id,
                    requested_model=requested_model)
        raise _err_403("model_not_allowed")

    def _servable(model: str) -> bool:
        # The requested model is exempt: the route already validated it.
        if model == requested_model:
            return True
        try:
            entry = _resolve_registry(model)
        except ValueError:
            logger.warning("cascade_candidate_unresolvable",
                           tenant_id=tenant_cfg and getattr(tenant_cfg, "tenant_id", None),
                           candidate=model)
            return False
        if wire_protocol is not None and entry.wire_protocol != wire_protocol:
            logger.warning("cascade_candidate_wrong_protocol",
                           candidate=model, wire_protocol=entry.wire_protocol,
                           route_protocol=wire_protocol)
            return False
        if getattr(entry, "served_by", "bedrock") == "vllm":
            # A vLLM candidate is servable only when hybrid serving is on AND its
            # endpoint is allowlisted; otherwise the cascade must skip it (flag
            # off => byte-identical to today, since no shipped entry is vLLM).
            from .serving.vllm import endpoint_is_servable
            if not endpoint_is_servable(entry.endpoint_key):
                logger.warning("cascade_candidate_vllm_unservable", candidate=model)
                return False
        return True

    servable = [m for m in candidates if _servable(m)]
    if not servable:
        # The requested model is exempt from the protocol drop, so reaching here
        # means every candidate — including the requested one when it was
        # admissible — cannot be served on this route. Serving the requested model
        # anyway is how an allowlist was escaped: it was policy-filtered OUT above
        # and then re-admitted here as a fallback.
        logger.info("no_servable_candidate", tenant_id=_tenant_id,
                    requested_model=requested_model, wire_protocol=wire_protocol)
        raise _err_403("model_not_allowed")
    return servable


def reserve_credit(
    user,
    reservation_tokens: int,
    *,
    pricing_key: Optional[str] = None,
    cost_microusd: Optional[int] = None,
    quota_lines: Optional[list] = None,
    quota_model: Optional[str] = None,
    selected_model: Optional[str] = None,
    rate_snapshot: Optional["RateSnapshot"] = None,
    payload_hash: Optional[str] = None,
    payload_bytes: Optional[int] = None,
    bound_microusd: Optional[int] = None,
) -> ReservationContext:
    """Atomically reserve budget before invoking Bedrock.

    - Without a tenant pool budget (or when `cost_microusd` is not supplied):
      debits `reservation_tokens` from the per-user balance exactly as before.
    - With a tenant pool budget for the current period: debits the per-user
      tokens AND reserves `cost_microusd` from the pool in one transaction.

    `rate_snapshot`, when supplied, is a RateSnapshot the CALLER already froze
    (docs/design/hard-ceiling.md item 5b: "settle at the reserve-time rates" —
    see `reserve_credit_for_model`'s `_price`). Using it here instead of
    freezing a second, independent snapshot is what makes that rule airtight
    rather than merely usual: the hard-ceiling bound and the settle-time
    charge are then GUARANTEED to price at the identical rate row, because
    they read it exactly once, not twice at slightly different instants. When
    omitted (every other/legacy caller), the snapshot is frozen here as
    before — unchanged behaviour.

    `bound_microusd`, when supplied and not equal to `cost_microusd`, is the
    sound bound `cost_microusd` did NOT get priced at — the `shadow_mode`
    case (`reserve_credit_for_model`'s `_price`): the pool/quota debit below
    still uses `cost_microusd` exactly as always (that is what actually gates
    admission), but `ReservationContext.measured_bound_microusd` records
    `bound_microusd` instead, because recording the bound is the entire point
    of shadow mode. Omitted (every other caller, and every state but
    `shadow`), `measured_bound_microusd` defaults to `cost_microusd` — bound
    and reserved coincide, unchanged from before this parameter existed.

    Returns a `ReservationContext` for the settle step. Raises HTTP 402 with a
    `reason` of `personal_budget_exhausted` or `tenant_pool_exhausted`.
    """
    _measured_bound_microusd = (
        int(bound_microusd) if bound_microusd is not None
        else (int(cost_microusd) if cost_microusd is not None else None)
    )
    repo = UserTenantsRepository()
    # Admission READS authority; it does not create it. This used to call
    # `ensure()`, which creates the membership and seeds it with the tenant's
    # default credit — so an identity with a profile row but no membership (an admin
    # creation that crashed between its two writes, or a row written out of band)
    # was granted a budget by making a request. Provisioning happens in the admin
    # API and the SSO exchange, both of which say which tenant and how much.
    if repo.get(user.user_id, user.org_id) is None:
        logger.info("identity_not_provisioned", user_id=user.user_id,
                    tenant_id=user.org_id)
        raise _err_403("identity_not_provisioned")

    period = current_period()
    # Layer 5: freeze the rate NOW (reserve time) so settle rates the charge at
    # the admitted version even if the live table is flipped later. Only when a
    # pricing_key is known (priced reservation); a rate-table blip must never fail
    # the reserve, so a snapshot failure degrades to None (settle then falls back
    # to the legacy live-rate path). Shared across every context return below.
    from .pricing import UNVERSIONED_SENTINEL as _UNVERSIONED

    if rate_snapshot is not None:
        _rate_snap = rate_snapshot
    else:
        _rate_snap = None
        if pricing_key:
            try:
                from .pricing import snapshot_rates
                _rate_snap = snapshot_rates(pricing_key)
            except Exception as e:  # noqa: BLE001 — one refusal, stated once.
                # There is no honest reservation without the rate it was admitted
                # at. This used to degrade: the terminal was labelled
                # `snapshot-failed` and the settle charged from the live table, so
                # a rate edit between admission and settle changed what the request
                # was charged — the one thing freezing exists to prevent. A rate
                # the gateway cannot read is a request it cannot price.
                logger.error("RateSnapshotFailed", pricing_key=pricing_key,
                             error=str(e))
                raise _err_503("pricing_unavailable") from None
    budgets = TenantBudgetsRepository()
    pool = budgets.get(user.org_id, period) if cost_microusd is not None else None

    # No pool budget AND no per-model quota to enforce → original single-table
    # fast path (fully backward compat).
    if (pool is None or cost_microusd is None) and not quota_lines:
        try:
            repo.reserve(
                user_id=user.user_id,
                tenant_id=user.org_id,
                tokens=reservation_tokens,
            )
        except CreditExhaustedError:
            remaining = repo.remaining_credit(user.user_id, user.org_id)
            logger.info(
                "credit_exhausted_402",
                user_id=user.user_id,
                tenant_id=user.org_id,
                remaining_credit=remaining,
                reservation_required=reservation_tokens,
                reason="personal_budget_exhausted",
            )
            raise _err_402("personal_budget_exhausted")
        return ReservationContext(
            tenants_repo=repo,
            reservation_tokens=reservation_tokens,
            period=period,
            pricing_key=pricing_key,
            rate_snapshot=_rate_snap,
            tenant_id=user.org_id,
            pool_active=False,
            selected_model=selected_model,
            measured_bound_microusd=_measured_bound_microusd,
        )

    # No pool budget but a per-model quota IS configured → enforce the quota
    # atomically alongside the per-user token reserve, WITHOUT a pool debit.
    # (Fable F-3: quota enforcement must not be coupled to having a pool — a
    # pool-less tenant with a per-model quota was previously served unmetered.)
    if pool is None or cost_microusd is None:
        return _reserve_quota_without_pool(
            user, reservation_tokens, repo=repo, period=period,
            pricing_key=pricing_key, quota_lines=quota_lines,
            quota_model=quota_model, selected_model=selected_model,
            quota_reserved_amount=int(cost_microusd or 0),
            bound_microusd=bound_microusd,
        )

    # Pool budget present → atomic two-table reservation. Both the per-user
    # balance and the pool are debited with snapshot optimistic locks inside a
    # single TransactWriteItems; a lost race cancels the whole transaction and
    # we retry with a fresh read. Ceiling checks are done in Python (DynamoDB
    # ConditionExpression cannot portably add attributes), then the commit is
    # gated on the snapshot values being unchanged — which is what makes it
    # race-safe.
    cost = int(cost_microusd)
    client = _low_level_client()
    # Best-effort reclaim of holds abandoned by dead requests before we take a
    # fresh snapshot, so this reservation locks on (and can use) budget that
    # orphaned holds were needlessly tying up. Never blocks or fails the request.
    _sweep_expired_holds(budgets, user.org_id, period)
    pool_vanished = False
    saw_throttle = False
    # One hold identity for this logical reservation, stable across our explicit
    # retries: a cancelled transaction writes nothing (the hold Put included), so
    # reusing the id on the next attempt cannot collide with a prior commit, and
    # a lost-ack on a real commit is deduped by botocore's same-token retry
    # before it could ever reach this loop again. The SK embeds the expiry, so
    # settle/release delete by this exact string rather than reconstructing it.
    hold_id = _fresh_idempotency_token()
    hold_expires_at = int(time.time()) + _HOLD_TTL_SECONDS
    hold_sk = _hold_sk(period, hold_expires_at, hold_id)
    # This loop's blocking boto3 calls + time.sleep are safe on the request
    # thread: the /v1/messages and /v1/chat/completions handlers are sync
    # `def`, so FastAPI runs them (and this reserve) on the threadpool, NOT the
    # event loop. (The settle at the tail runs inside an async generator on the
    # loop and IS offloaded to a thread — see _budget_flow.run_stream.)
    for _attempt in range(_RESERVE_MAX_RETRIES):
        if _attempt:
            # Full-jitter exponential backoff so a thundering herd on one hot
            # pool row desynchronises instead of colliding in lockstep every
            # attempt. Linear backoff let a 20-way concurrent burst on one
            # tenant exhaust all retries and fail closed (503); jittered
            # exponential keeps the snapshot lock making progress under the
            # same contention. Still fails closed if it truly can't win — a
            # pooled tenant must never slip through unpriced.
            time.sleep(_contention_backoff(_attempt))

        # ConsistentRead: the snapshot we lock on MUST be current, or a stale
        # eventually-consistent read yields expected_* values that no longer
        # match and the transaction cancels forever. moto is always strongly
        # consistent so this only matters against real DynamoDB — which is
        # exactly where the fail-open used to bite.
        item = repo.get(user.user_id, user.org_id, consistent_read=True)
        if not item:
            raise _err_402("personal_budget_exhausted")
        total = int(item.get("total_credit", 0))
        used = int(item.get("credit_used", 0))
        if used + reservation_tokens > total:
            logger.info(
                "credit_exhausted_402",
                user_id=user.user_id,
                tenant_id=user.org_id,
                reason="personal_budget_exhausted",
            )
            raise _err_402("personal_budget_exhausted")

        pool_row = budgets.get(user.org_id, period, consistent_read=True)
        if pool_row is None:
            # Pool row genuinely deleted mid-flight → the tenant is now unlimited
            # at the pool level, so per-user-only budgeting is the correct
            # behaviour. This is the ONLY path allowed to drop the pool debit.
            pool_vanished = True
            break

        # Extracted before any refusal path below, so EVERY refusal — suspended,
        # oversized, or ordinary exhaustion — can log the reserved/settled split
        # at that moment (docs/design/hard-ceiling.md item 2b, third bullet): "a
        # refusal with high reserved and low settled is the aggregate case,
        # and it is the signal an operator needs to tell 'my budget is spent'
        # from 'my budget is tied up in flight'." Without logging this on
        # EVERY refusal path, that distinction is invisible exactly when it
        # matters most.
        p_limit = int(pool_row.get("pool_limit_microusd", 0))
        p_reserved = int(pool_row.get("pool_reserved_microusd", 0))
        p_settled = int(pool_row.get("pool_settled_microusd", 0))

        # A suspended pool must reject immediately. Without this the reserve
        # transaction's `status = active` condition fails every attempt, retries
        # exhaust, and (previously) the request slipped through per-user-only —
        # turning "tenant suspended" into "tenant billed off-pool". Fail closed.
        if str(pool_row.get("status", "active")) != "active":
            logger.info(
                "credit_exhausted_402",
                user_id=user.user_id,
                tenant_id=user.org_id,
                reason="tenant_pool_exhausted",
                pool_status=str(pool_row.get("status")),
                pool_reserved_microusd=p_reserved,
                pool_settled_microusd=p_settled,
                pool_limit_microusd=p_limit,
            )
            raise _err_402("tenant_pool_exhausted")

        # docs/design/hard-ceiling.md item 2b: two DISTINCT conditions, reported
        # differently. An earlier version of this check tested the bound
        # against a FRACTION of pool_limit and refused on that — review found
        # that wrong in both directions: it fires when the request would in
        # fact fit the current headroom comfortably, and stays silent in
        # exactly the scenario it exists for (many mid-sized concurrent
        # requests, each under the fraction, collectively exhausting the
        # headroom). So:
        #
        #   1. "Cannot fit at all" — bound > the WHOLE pool_limit. Exact, no
        #      fraction: no amount of waiting or draining ever admits this
        #      request to this budget, so it is refused with a reason distinct
        #      from ordinary exhaustion. This is acceptance criterion 3's
        #      first half; criterion 3 as a whole is NOT claimed for the
        #      strict-only first slice (section 12 says does-not-fit and
        #      exhausted may share one refusal there) — implemented anyway
        #      because it is exact, cheap, and strictly more informative than
        #      collapsing the two, never less. Checked before the headroom
        #      test so it never burns a retry attempt discovering the pool
        #      was "exhausted" by exactly this one reservation, and never
        #      competes for headroom it could never have used anyway.
        #   2. "Will monopolise the budget" — bound exceeds the CONFIGURED
        #      FRACTION of pool_limit while still fitting under it. This can
        #      legitimately succeed, so it is NOT refused — only warned, naming
        #      the tenant and the ratio, because it means the budget is sized
        #      for fewer concurrent requests than the workload wants.
        if p_limit > 0 and cost > p_limit:
            logger.warning(
                "request_does_not_fit_pool_limit",
                user_id=user.user_id,
                tenant_id=user.org_id,
                period=period,
                reservation_microusd=cost,
                pool_limit_microusd=p_limit,
                pool_reserved_microusd=p_reserved,
                pool_settled_microusd=p_settled,
            )
            raise _err_402_does_not_fit("request_does_not_fit_pool_limit")
        if p_limit > 0 and cost > p_limit * _MAX_RESERVATION_FRACTION_OF_POOL:
            logger.warning(
                "reservation_will_monopolise_pool",
                user_id=user.user_id,
                tenant_id=user.org_id,
                period=period,
                reservation_microusd=cost,
                pool_limit_microusd=p_limit,
                max_fraction=_MAX_RESERVATION_FRACTION_OF_POOL,
            )

        if p_reserved + p_settled + cost > p_limit:
            logger.info(
                "credit_exhausted_402",
                user_id=user.user_id,
                tenant_id=user.org_id,
                reason="tenant_pool_exhausted",
                pool_reserved_microusd=p_reserved,
                pool_settled_microusd=p_settled,
                pool_limit_microusd=p_limit,
                reservation_microusd=cost,
            )
            raise _err_402("tenant_pool_exhausted")

        user_txn = repo.reserve_txn_item(
            user_id=user.user_id,
            tenant_id=user.org_id,
            tokens=reservation_tokens,
            expected_total=total,
        )
        pool_txn = budgets.reserve_txn_item(
            tenant_id=user.org_id,
            period=period,
            amount_microusd=cost,
        )
        hold_txn = budgets.hold_put_txn_item(
            tenant_id=user.org_id,
            period=period,
            hold_id=hold_id,
            amount_microusd=cost,
            expires_at_epoch=hold_expires_at,
            # C-1 (two-item migration): tag inline LLM holds so the external
            # capture/void gate — which reads the HOLD's `source` once it goes
            # HOLD-only — DENIES them. An inline hold is never externally
            # capturable/voidable; without this tag it would rely on the RESERVE
            # event's source, which is exactly what that step stops reading.
            source="inline",
            # docs/design/hard-ceiling.md section 3a: pin the canonical payload's
            # hash + byte length onto the hold itself (not just the in-memory
            # ReservationContext), so they survive a process restart and are
            # independently readable — the hold, not the request, is this
            # reservation's durable record.
            payload_hash=payload_hash,
            payload_bytes=payload_bytes,
        )
        txn_items = [user_txn, pool_txn, hold_txn]
        _quota_start = len(txn_items)
        _quota_count = 0
        if quota_lines:
            txn_items.extend(quota_lines)
            _quota_count = len(quota_lines)
        # RESERVE ledger event LAST, so the fixed pool/user/hold/quota indices the
        # cancellation parsing relies on are unchanged. Its attribute_not_exists
        # can only CCF on a hold_id collision (uuid → never in practice), and the
        # quota scan is bounded to the quota slice so a ledger CCF is never
        # misread as quota-exhausted. Positive reserved_delta makes the reserved
        # side ledger-derivable (I2).
        if hold_id:
            txn_items.append(
                _reaper_ledger().reserve_event_txn_item(
                    tenant_id=user.org_id,
                    period=period,
                    hold_id=hold_id,
                    reserved_delta_microusd=int(cost),
                    run_id=hold_id,
                    run_id_is_fallback=True,
                    model_id=selected_model,
                    # Layer 5: the frozen VERSION (bug#1 fix), and the full rate
                    # snapshot serialized so a cross-process recovery can restore
                    # it (Fable review H1). Distinct sentinel per cause when no
                    # snapshot was frozen (review-2 N2/N3).
                    # Layer 5: the frozen VERSION. A priced reservation always
                    # carries a snapshot now (pricing fails closed), so the only
                    # remaining sentinel is the honest one for a reservation that
                    # was never priced at all.
                    pricing_version=(
                        _rate_snap.version if _rate_snap is not None else _UNVERSIONED
                    ),
                    rate_snapshot=(
                        _rate_snap.to_ledger_dict() if _rate_snap is not None else None
                    ),
                )
            )
        try:
            client.transact_write_items(
                TransactItems=txn_items,
                # Fresh token per attempt: dedupes only botocore's transparent
                # retry of THIS transact call (so a lost ack cannot double-debit)
                # while staying distinct from every concurrent caller and from
                # our own next retry — each of which is a genuinely different
                # write (new snapshot / new updated_at).
                ClientRequestToken=_fresh_idempotency_token(),
            )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code != "TransactionCanceledException":
                raise
            # Inspect why it cancelled. ConditionalCheckFailed = a concurrent
            # reserve/settle moved a snapshot value → retry with a fresh read. A
            # throttle/conflict = transient capacity → also retry, but remember
            # it so that if we ultimately give up we surface a retryable 503
            # rather than a misleading 402 "out of budget".
            reasons = e.response.get("CancellationReasons", []) or []
            codes = {r.get("Code", "") for r in reasons}
            # txn_items order is [user_txn(0), pool_txn(1), hold_txn(2),
            # *quota_lines(_quota_start..), RESERVE ledger(last)]. A
            # ConditionalCheckFailed at a QUOTA index means the per-model quota is
            # exhausted — NOT a snapshot race — so retrying would fail forever.
            # Surface QuotaExhausted so the caller's cascade advances to the next
            # model. (pool/user indices 0-1 are the retryable race; index 2 is the
            # hold_id collision guard; the trailing RESERVE ledger item is scanned
            # separately below.) The quota scan is bounded to EXACTLY the quota
            # slice so the appended ledger item's index is never misread as quota.
            if quota_model is not None and _quota_count:
                for r in reasons[_quota_start:_quota_start + _quota_count]:
                    if r.get("Code", "") == "ConditionalCheckFailed":
                        logger.info(
                            "model_quota_exhausted",
                            tenant_id=user.org_id, model=quota_model, period=period,
                        )
                        raise QuotaExhausted(quota_model)
            if codes & {
                "ThrottlingError",
                "ProvisionedThroughputExceeded",
                "TransactionConflict",
                "RequestLimitExceeded",
            }:
                saw_throttle = True
            continue

        return ReservationContext(
            tenants_repo=repo,
            reservation_tokens=reservation_tokens,
            pool_reserved_microusd=cost,
            period=period,
            pricing_key=pricing_key,
            rate_snapshot=_rate_snap,
            tenant_id=user.org_id,
            pool_active=True,
            hold_id=hold_id,
            hold_sk=hold_sk,
            quota_lines=quota_lines,
            selected_model=selected_model,
            quota_reserved_amount=cost if quota_lines else 0,
            quota_user_id=user.user_id,
            quota_period=period if quota_lines else None,
            measured_bound_microusd=_measured_bound_microusd,
        )

    # Pool row deleted mid-flight → per-user-only reservation is correct.
    if pool_vanished:
        # Pool disappeared mid-flight → no pool ceiling, but a configured
        # per-model quota still applies. Route through the same quota-only path
        # so quota is enforced and `selected_model` is set (Fable F-3).
        if quota_lines:
            return _reserve_quota_without_pool(
                user, reservation_tokens, repo=repo, period=period,
                pricing_key=pricing_key, quota_lines=quota_lines,
                quota_model=quota_model, selected_model=selected_model,
                quota_reserved_amount=int(cost_microusd or 0),
                bound_microusd=bound_microusd,
            )
        try:
            repo.reserve(
                user_id=user.user_id,
                tenant_id=user.org_id,
                tokens=reservation_tokens,
            )
        except CreditExhaustedError:
            raise _err_402("personal_budget_exhausted")
        return ReservationContext(
            tenants_repo=repo,
            reservation_tokens=reservation_tokens,
            period=period,
            pricing_key=pricing_key,
            rate_snapshot=_rate_snap,
            tenant_id=user.org_id,
            pool_active=False,
            selected_model=selected_model,
            measured_bound_microusd=_measured_bound_microusd,
        )

    # Retries exhausted under contention while the pool was still present. We
    # MUST NOT fall back to per-user-only here — doing so lets a request slip
    # past the pool ceiling exactly when the pool is hottest (near its limit).
    # Fail closed. A throttle-driven exhaustion is transient capacity, so
    # surface a retryable 503; otherwise the caller genuinely lost every race
    # for the last slice of budget, which is a 402.
    logger.warning(
        "pool_reserve_retries_exhausted",
        user_id=user.user_id,
        tenant_id=user.org_id,
        period=period,
        attempts=_RESERVE_MAX_RETRIES,
        throttled=saw_throttle,
    )
    if saw_throttle:
        raise HTTPException(
            status_code=503,
            detail={
                "type": "budget_unavailable",
                "reason": "pool_reservation_contended",
                "message": (
                    "Budget reservation is temporarily unavailable. "
                    "Retry shortly."
                ),
            },
        )
    raise _err_402("tenant_pool_exhausted")


def reserve_external_authorization(
    *,
    tenant_id: str,
    amount_microusd: int,
    idempotency_key: str,
    request_fingerprint: str,
    authorization_id_factory,
    ttl_seconds: int,
    pricing_key: Optional[str] = None,
    rate_snapshot: Optional["RateSnapshot"] = None,
    description: Optional[str] = None,
    workflow_run_id: Optional[str] = None,
) -> "ExternalAuthorizeResult":
    """Reserve `amount_microusd` from a tenant's pool for an EXTERNAL authorize
    (Fable authcap). Pool-only: unlike an inline request there is no per-user
    token debit and no per-model quota — an external action is not token-metered,
    it is a flat dollar hold the tenant will later `capture` (settle) or `void`
    (release) from a SEPARATE HTTP call.

    The transaction is [pool reserve (CAS), HOLD put, RESERVE ledger event
    (source=external, carries the frozen rate_snapshot + description), IDEMP
    record]. Every item is an EXISTING, reviewed primitive — the only new money
    behaviour is the IDEMP Put, which rides `attribute_not_exists(pk)` so
    "IDEMP row exists ⟺ this reserve committed" is atomic. A duplicate
    Idempotency-Key CCFs the whole txn → we read the prior IDEMP row and REPLAY
    its authorization (idempotent authorize, Fable authcap A/C).

    Same snapshot-optimistic CAS + full-jitter retry as `reserve_credit`'s pooled
    path, and it FAILS CLOSED the same way (a pooled tenant must never get an
    unpriced hold). Raises HTTP 402 `tenant_pool_exhausted` (no room / suspended),
    404-mapped `no_pool` (the tenant has no pool for the period — external
    authorize requires one), or 503 on sustained contention.

    `authorization_id_factory(hold_id, period, hold_sk) -> str` mints the opaque
    authorization id from the hold identity (all known BEFORE the txn), so the
    real id is stored in the IDEMP row at write time — no placeholder + rewrite,
    and a duplicate-key replay recomputes the SAME id deterministically.
    """
    period = current_period()
    budgets = TenantBudgetsRepository()
    amount = int(amount_microusd)
    if amount <= 0:
        raise _err_400("amount_must_be_positive")

    ledger = _reaper_ledger()
    # A retried authorize with the SAME Idempotency-Key that already committed is
    # the common duplicate — detect it up front with a consistent read so we
    # replay without even attempting a reserve. (The txn's IDEMP
    # attribute_not_exists is still the AUTHORITATIVE guard against a
    # read-then-write race; this is just a fast path.)
    #
    # PERIOD BOUNDARY (Fable authcap review-1 H-2): the IDEMP row's pk embeds the
    # period the authorize committed in. A retry that crosses a month boundary
    # (authorize at 23:59, retry at 00:01) computes a NEW period, so a
    # current-period-only lookup would miss the prior row and mint a SECOND hold
    # for the same key. Since ttl_max is 24h, the original can be at most one
    # period back, so we also check the previous period. A hit there replays the
    # ORIGINAL (correctly settling against the period it reserved in).
    prior = _read_idemp_with_prev_period(ledger, tenant_id, period, idempotency_key)
    if prior is not None:
        # The row's PRESENCE is not the same fact under both protocols, and this is
        # the seam where that used to be assumed. The transactional path writes the
        # row inside the reserve transaction, so its presence means the debit
        # committed. The PENDING path writes it BEFORE the commit point, so its
        # presence means only that an attempt began — and replaying it as an
        # authorization handed back a live authorization_id for a debit that may
        # have been refused (C5.4). `_replay_committed_or_refuse` asks the durable
        # state instead of the row, so one resolver answers for both protocols.
        return _replay_committed_or_refuse(
            budgets, ledger, prior, request_fingerprint,
            tenant_id=tenant_id, idempotency_key=idempotency_key)

    # PENDING protocol dispatch (docs/design/pending-protocol.md). Default
    # "transaction" falls through to the unchanged 4-item path below; a canary
    # tenant (allowlist) or a global "pending" flag takes the marker path.
    if _reserve_protocol_for(tenant_id) == "pending":
        return _reserve_external_pending(
            tenant_id=tenant_id, period=period, amount=amount,
            idempotency_key=idempotency_key, request_fingerprint=request_fingerprint,
            authorization_id_factory=authorization_id_factory,
            ttl_seconds=ttl_seconds, pricing_key=pricing_key,
            rate_snapshot=rate_snapshot, description=description,
            workflow_run_id=workflow_run_id, budgets=budgets, ledger=ledger,
        )

    client = _low_level_client()
    _sweep_expired_holds(budgets, tenant_id, period)
    saw_throttle = False
    hold_id = _fresh_idempotency_token()
    hold_expires_at = int(time.time()) + max(int(ttl_seconds), 0)
    hold_sk = _hold_sk(period, hold_expires_at, hold_id)
    authorization_id = authorization_id_factory(hold_id, period, hold_sk)
    rate_snapshot_dict = rate_snapshot.to_ledger_dict() if rate_snapshot is not None else None
    capture_mode = "amount" if pricing_key is None else "units"
    for _attempt in range(_RESERVE_MAX_RETRIES):
        if _attempt:
            time.sleep(_contention_backoff(_attempt))
        pool_row = budgets.get(tenant_id, period, consistent_read=True)
        if pool_row is None:
            # External authorize requires a pool to reserve against — there is no
            # per-user token fallback for a non-request charge. Surface a distinct
            # reason the endpoint maps to 404 (no pool configured).
            raise ExternalAuthorizeNoPool(tenant_id, period)
        if str(pool_row.get("status", "active")) != "active":
            raise _err_402("tenant_pool_exhausted")
        p_limit = int(pool_row.get("pool_limit_microusd", 0))
        p_reserved = int(pool_row.get("pool_reserved_microusd", 0))
        p_settled = int(pool_row.get("pool_settled_microusd", 0))
        if p_reserved + p_settled + amount > p_limit:
            raise _err_402("tenant_pool_exhausted")

        pool_txn = budgets.reserve_txn_item(
            tenant_id=tenant_id,
            period=period,
            amount_microusd=amount,
        )
        # HOLD enrichment (two-item migration step 2, dual-write phase): the HOLD
        # row now carries source/description/rate_snapshot/payload_hash so a later
        # step can move capture/void to read the HOLD ALONE (and the RESERVE event
        # async). The RESERVE event is STILL written synchronously below in this
        # phase; capture/void keeps reading it until the dual-read cutover proves
        # HOLD-only is equivalent (see docs/design/ledger-hot-path.md).
        hold_txn = budgets.hold_put_txn_item(
            tenant_id=tenant_id,
            period=period,
            hold_id=hold_id,
            amount_microusd=amount,
            expires_at_epoch=hold_expires_at,
            source="external",
            description=description,
            rate_snapshot=rate_snapshot_dict,
            payload_hash=request_fingerprint,
            run_id=workflow_run_id or hold_id,
            run_id_is_fallback=workflow_run_id is None,
        )
        # TransactItems ORDER: [pool(0), hold(1), RESERVE(2), IDEMP(3)]. Only the
        # IDEMP item's CCF is interpreted specially (duplicate key); a pool(0) CCF
        # is the retryable snapshot race; a hold(1) CCF is the uuid-collision guard
        # (never in practice).
        reserve_evt = ledger.reserve_event_txn_item(
            tenant_id=tenant_id,
            period=period,
            hold_id=hold_id,
            reserved_delta_microusd=amount,
            run_id=workflow_run_id or hold_id,
            run_id_is_fallback=workflow_run_id is None,
            pricing_version=(rate_snapshot.version if rate_snapshot is not None else None),
            rate_snapshot=rate_snapshot_dict,
            source="external",
            description=description,
        )
        idemp_txn = ledger.idemp_txn_item(
            tenant_id=tenant_id,
            period=period,
            idempotency_key=idempotency_key,
            hold_id=hold_id,
            hold_sk=hold_sk,
            authorization_id=authorization_id,
            amount_microusd=amount,
            expires_at_epoch=hold_expires_at,
            capture_mode=capture_mode,
            request_fingerprint=request_fingerprint,
            pricing_key=pricing_key,
        )
        _IDEMP_IDX = 3
        _txn_t0 = time.perf_counter()
        try:
            client.transact_write_items(
                TransactItems=[pool_txn, hold_txn, reserve_evt, idemp_txn],
                ClientRequestToken=_fresh_idempotency_token(),
            )
            # Ledger-write latency telemetry (permanent): the synchronous
            # TransactWriteItems is THE cost of putting the ledger on the hot
            # path, so we log its wall-clock ms so a metric filter / benchmark can
            # separate "ledger round-trip" from the HTTP/ALB shell. Only the
            # committed path is timed; a CCF/throttle falls through to the retry
            # loop below and is not a settled ledger write. Guarded — telemetry
            # must never break a reserve.
            try:
                logger.info(
                    "ledger_transact_latency",
                    op="external_authorize_reserve",
                    duration_ms=round((time.perf_counter() - _txn_t0) * 1000, 3),
                    tenant_id=tenant_id,
                )
            except Exception:  # noqa: BLE001
                pass
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code != "TransactionCanceledException":
                raise
            reasons = _cancellation_codes(e)
            # A duplicate Idempotency-Key: the IDEMP Put CCF'd. A concurrent
            # authorize with the same key beat us (or a prior commit did) → read
            # the winning IDEMP row and REPLAY its authorization. This is the
            # read-then-write race the txn-level guard closes: whoever won wrote
            # exactly one hold, and every racer returns that same authorization_id.
            if (
                len(reasons) > _IDEMP_IDX
                and reasons[_IDEMP_IDX] == "ConditionalCheckFailed"
            ):
                winner = _read_idemp_with_prev_period(
                    ledger, tenant_id, period, idempotency_key
                )
                if winner is not None:
                    return _replay_committed_or_refuse(
                        budgets, ledger, winner, request_fingerprint,
                        tenant_id=tenant_id, idempotency_key=idempotency_key)
                # CCF but no readable row: get_idemp is ConsistentRead, so a CCF
                # with no readable winner is a genuine transient (throttle) → retry
                # (and count it as a throttle so exhaustion surfaces 503, not a
                # misleading 402 — Fable review-1 M-3).
                saw_throttle = True
            # hold(1) CCF = a uuid collision (astronomically rare). Retrying with
            # the SAME hold_id would CCF forever → re-mint the hold identity so the
            # next attempt uses a fresh one (Fable review-1 Low). The
            # authorization_id is derived from hold_id, so re-derive it too.
            if (
                len(reasons) > 1
                and reasons[1] == "ConditionalCheckFailed"
                and (len(reasons) <= _IDEMP_IDX or reasons[_IDEMP_IDX] != "ConditionalCheckFailed")
            ):
                hold_id = _fresh_idempotency_token()
                hold_sk = _hold_sk(period, hold_expires_at, hold_id)
                authorization_id = authorization_id_factory(hold_id, period, hold_sk)
            if {
                "ThrottlingError",
                "ProvisionedThroughputExceeded",
                "TransactionConflict",
                "RequestLimitExceeded",
            } & set(reasons):
                saw_throttle = True
            continue
        return ExternalAuthorizeResult(
            authorization_id=authorization_id,
            hold_id=hold_id,
            hold_sk=hold_sk,
            period=period,
            amount_microusd=amount,
            expires_at_epoch=hold_expires_at,
            capture_mode=capture_mode,
            replayed=False,
        )

    logger.warning(
        "external_authorize_retries_exhausted",
        tenant_id=tenant_id, period=period,
        attempts=_RESERVE_MAX_RETRIES, throttled=saw_throttle,
    )
    if saw_throttle:
        raise HTTPException(
            status_code=503,
            detail={
                "type": "budget_unavailable",
                "reason": "pool_reservation_contended",
                "message": "Budget reservation is temporarily unavailable. Retry shortly.",
            },
        )
    raise _err_402("tenant_pool_exhausted")


def _pending_hold_id(tenant_id: str, period: str, idempotency_key: str) -> str:
    """Deterministic hold_id from the Idempotency-Key.

    I6 in docs/design/pending-protocol.md. Step 1's `attribute_not_exists(sk)` doubles as the
    duplicate-Key detector: a replay derives the SAME hold_id → the SAME sk →
    collides, so no second hold or debit is created. Namespaced by tenant+period
    so the same key in different tenants/periods never collides."""
    ns = uuid.uuid5(uuid.NAMESPACE_URL, f"stratoclave/pending/{tenant_id}/{period}")
    return uuid.uuid5(ns, idempotency_key).hex


class _AmbiguousReserve(Exception):
    """Step 2 (the pool debit) returned an ambiguous outcome (timeout / 5xx): it
    MAY or MAY NOT have applied. Per I4 the debit is NEVER resent; per the leak-
    safe rule the hold is abandoned PENDING (the sweeper fences it, the reconciler
    recovers any real debit in aggregate) and the caller surfaces a 503 so the
    client retries with a NEW Idempotency-Key rather than us re-debiting."""


# Minimum TTL for a PENDING hold (Fable review, bug 6): the sweeper's fence must
# never race step 3, so the hold's lifetime must dominate the step-3 retry
# horizon. A ttl of 0 (or a few seconds) would let a fence beat activate. The
# authorize endpoint already clamps ttl to >= 30s, but pending re-asserts a floor
# so a mis-wired caller cannot create a self-fencing hold.
_PENDING_MIN_TTL_SECONDS = 30


def _pending_commit_transact(budgets, *, tenant_id, period, hold_id, amount) -> str:
    """Execute the PENDING-protocol COMMIT POINT: the 2-item pool-debit + marker-Put
    TransactWriteItems (docs/design/pending-protocol.md, PR-1). Returns one of
    budgets.RESERVE_APPLIED / RESERVE_ALREADY / RESERVE_EXHAUSTED. Raises
    ExternalAuthorizeNoPool if the pool row is missing, and a 503 HTTPException on an
    ambiguous outcome that even a ConsistentRead of the marker cannot resolve.

    Cancellation-reason contract (Fable PR-1 Q4-item-2): the items are [pool(0),
    marker(1)]. On TransactionCanceledException the MARKER reason is inspected
    FIRST: a marker-side ConditionalCheckFailed means this hold already committed
    (idempotent success, RESERVE_ALREADY) — reversing the order would return 402 to
    a client whose debit already landed, and it would re-reserve under a new hold_id
    = a double debit. Only if the marker is fine and the POOL reason is CCF do we
    return RESERVE_EXHAUSTED (genuine budget exhaustion → 402)."""
    # RESERVE ORACLE (docs/design/pending-protocol.md, golden-reference migration):
    # when enabled, snapshot the pool's pre-state so we can compare the write-set
    # pending is about to send against what the FROZEN transaction golden would
    # produce. ONE strongly-consistent read (the same the golden path does), gated
    # by the flag → zero cost when off. NEVER changes control flow.
    _oracle_pool_row = None
    _oracle_on = False
    try:
        from . import reserve_oracle as _ro
        _oracle_on = _ro.oracle_enabled()
        if _oracle_on:
            _oracle_pool_row = budgets.get(tenant_id, period, consistent_read=True)
    except Exception:  # noqa: BLE001 — the oracle must never break a reserve
        _oracle_on = False

    items = budgets.reserve_commit_txn_items(
        tenant_id=tenant_id, period=period, hold_id=hold_id, amount_microusd=amount)
    client = _low_level_client()

    def _oracle_check(outcome: str) -> None:
        """Compare pending's actual write-set to the golden's prediction. Best-
        effort, fail-open — logs a mismatch, never raises, never rolls back."""
        if not _oracle_on:
            return
        # RESERVE_ALREADY is an IDEMPOTENT replay of a hold whose debit landed on a
        # PRIOR call. The pool pre-state we snapshotted already reflects that debit,
        # so the golden's fresh-reserve prediction is not apples-to-apples here
        # (golden would 'reject a re-reserve' that pending idempotently admits).
        # The oracle checks FRESH admission equivalence only; skip replays.
        if outcome == budgets.RESERVE_ALREADY:
            return
        try:
            golden = _ro.golden_predicted_writeset(
                amount_microusd=amount, pool_row=_oracle_pool_row)
            pending = _ro.pending_actual_writeset(
                amount_microusd=amount, outcome=outcome,
                exhausted_sentinel=budgets.RESERVE_EXHAUSTED,
                applied_sentinel=budgets.RESERVE_APPLIED)
            # The equivalence check lives ENTIRELY in compare_and_log (no caller
            # pre-judge, so they can't drift). It calls `reread` ONLY on a
            # disagreement — a strongly-consistent post-commit pool read — to tell a
            # genuine mismatch from a benign TOCTOU race. A match pays no extra read.
            _ro.compare_and_log(
                tenant_id=tenant_id, period=period, hold_id=hold_id,
                golden=golden, pending=pending, pool_before=_oracle_pool_row,
                reread=lambda: budgets.get(tenant_id, period, consistent_read=True))
        except Exception:  # noqa: BLE001 — a detector must never fail the reserve
            pass

    try:
        # No ClientRequestToken: idempotency is the marker's attribute_not_exists,
        # not the 10-minute token dedup window.
        client.transact_write_items(TransactItems=items)
        _oracle_check(budgets.RESERVE_APPLIED)
        return budgets.RESERVE_APPLIED
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            raise ExternalAuthorizeNoPool(tenant_id, period)
        if code == "TransactionCanceledException":
            reasons = _cancellation_codes(e)
            marker_ccf = len(reasons) > 1 and reasons[1] == "ConditionalCheckFailed"
            pool_ccf = len(reasons) > 0 and reasons[0] == "ConditionalCheckFailed"
            if marker_ccf:
                # This hold's marker already exists → a prior attempt committed the
                # debit. Idempotent success; do NOT re-debit (I4).
                _oracle_check(budgets.RESERVE_ALREADY)
                return budgets.RESERVE_ALREADY
            if pool_ccf:
                _oracle_check(budgets.RESERVE_EXHAUSTED)
                return budgets.RESERVE_EXHAUSTED   # genuine exhaustion → 402
            # A TransactionConflict (optimistic serialization collision on the hot
            # pool item) or throttle: retryable. Fall through to ambiguity handling
            # below, which re-reads the marker — if a prior attempt actually landed
            # the debit, we report success; otherwise 503-retry-same-key.
        # AMBIGUOUS (timeout / 5xx / conflict): the write MAY have applied. Resolve
        # by reading the marker with a strongly-consistent GetItem BEFORE responding
        # (Fable PR-1 Q4-item-3 — never leave this to the reconciler, which would
        # leak). Marker present ⇒ the debit is a fact ⇒ success; absent ⇒ 503 so the
        # client retries the SAME Idempotency-Key (the marker dedupes, no double
        # debit).
        try:
            if budgets.pool_marker_amount(tenant_id=tenant_id, period=period,
                                          hold_id=hold_id) is not None:
                return budgets.RESERVE_ALREADY
        except Exception:  # noqa: BLE001 — a failed probe just defers to the 503 retry
            pass
        logger.warning("pending_reserve_ambiguous", tenant_id=tenant_id,
                       period=period, hold_id=hold_id, code=code)
        raise HTTPException(status_code=503, detail={
            "type": "budget_unavailable", "reason": "pool_reservation_ambiguous",
            "message": "Budget reservation could not be confirmed. Retry with the same Idempotency-Key."})


def _reserve_external_pending(
    *, tenant_id, period, amount, idempotency_key, request_fingerprint,
    authorization_id_factory, ttl_seconds, pricing_key, rate_snapshot,
    description, workflow_run_id, budgets, ledger,
) -> "ExternalAuthorizeResult":
    """The PENDING protocol reserve (docs/design/pending-protocol.md, Fable marker
    design). Non-transactional hot path whose money-safety comes from a per-hold
    marker (`applied.<hold_id>`) written ATOMICALLY with the pool debit in a single
    UpdateItem, so "did this hold's debit commit?" is a decisive, locally-readable
    fact (A1 restored without a transaction). Steps:

      0. IDEMP intent Put (persist hold_sk/amount/authorization_id) — the durable
         addressing a replay returns, and the duplicate-key detector.
      1. HOLD status=PENDING (write-ahead intent, precedes the debit).
      2. COMMIT: marker-carrying conditional UpdateItem on the pool. Its outcome
         (APPLIED / ALREADY / EXHAUSTED) is decisive; ambiguity is resolved by the
         marker, so an SDK retry is harmless and there is no fail-open (bug 7d).
      3. async PENDING -> ACTIVE (off the critical path).
    """
    _sweep_expired_holds(budgets, tenant_id, period)  # unchanged background reclaim
    hold_id = _pending_hold_id(tenant_id, period, idempotency_key)
    ttl = max(int(ttl_seconds), _PENDING_MIN_TTL_SECONDS)
    hold_expires_at = int(time.time()) + ttl
    hold_sk = _hold_sk(period, hold_expires_at, hold_id)
    authorization_id = authorization_id_factory(hold_id, period, hold_sk)
    rate_snapshot_dict = rate_snapshot.to_ledger_dict() if rate_snapshot is not None else None
    capture_mode = "amount" if pricing_key is None else "units"

    # STEP 0 (IDEMP intent): persist the addressing a replay must return. A
    # duplicate key CCFs here -> resolve by READING state (never assume success).
    try:
        ledger.put_idemp_intent(
            tenant_id=tenant_id, period=period, idempotency_key=idempotency_key,
            hold_id=hold_id, hold_sk=hold_sk, authorization_id=authorization_id,
            amount_microusd=amount, expires_at_epoch=hold_expires_at,
            capture_mode=capture_mode, request_fingerprint=request_fingerprint,
            pricing_key=pricing_key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        # The intent already exists: a concurrent attempt or an earlier one wrote
        # it. Its presence is not evidence the debit landed (that is the whole
        # shape of this protocol), so the same resolver the entry point uses reads
        # the durable state and decides.
        prior = _read_idemp_with_prev_period(
            ledger, tenant_id, period, idempotency_key)
        if prior is None:
            # The Put CCF'd but the row is not readable on a consistent read. That
            # is a transient, not a verdict — retrying the same key is safe because
            # the intent Put is itself conditional.
            raise HTTPException(status_code=503, detail={
                "type": "budget_unavailable", "reason": "pool_reservation_in_flight",
                "message": "A reservation for this Idempotency-Key is in flight. "
                           "Retry shortly."})
        return _replay_committed_or_refuse(
            budgets, ledger, prior, request_fingerprint,
            tenant_id=tenant_id, idempotency_key=idempotency_key)

    # STEP 1 (write-ahead intent): HOLD status=PENDING. A self SDK-retry / re-entry
    # CCFs; the marker read in step 2 makes that harmless (idempotent).
    try:
        budgets.hold_put_pending(
            tenant_id=tenant_id, period=period, hold_id=hold_id,
            amount_microusd=amount, expires_at_epoch=hold_expires_at,
            source="external", description=description,
            rate_snapshot=rate_snapshot_dict, payload_hash=request_fingerprint,
            run_id=workflow_run_id or hold_id, run_id_is_fallback=workflow_run_id is None,
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        # hold already exists (self-retry / re-entry): fall through to step 2,
        # which is idempotent via the marker.

    # STEP 2 (COMMIT POINT): a 2-item TransactWriteItems — pool conditional debit
    # + a SEPARATE fixed-size marker Put (docs/design/pending-protocol.md, PR-1;
    # supersedes the rejected marker-in-the-pool-item map). Atomic, so the debit
    # and its observable proof land together. Idempotent — a re-issue of the SAME
    # hold either applies once (marker absent) or the marker Put CCFs (already
    # applied). NO ClientRequestToken (Fable PR-1 Q4-item-1): the marker's
    # attribute_not_exists is the idempotency guarantee, and a stale token's 10-min
    # dedup window would misfire against our own retry window.
    _t0 = time.perf_counter()
    outcome = _pending_commit_transact(
        budgets, tenant_id=tenant_id, period=period, hold_id=hold_id, amount=amount)

    if outcome == budgets.RESERVE_EXHAUSTED:
        # Genuine exhaustion: no marker was written, pool untouched. Mark the
        # IDEMP intent + hold FAILED (leak-safe, replayable) and 402.
        # The HOLD's status is what a replay reads (`_replay_committed_or_refuse`), and
        # it lives on TenantBudgets where an update is permitted. There is no
        # matching mark on the ledger intent: that row is append-only, and the
        # status field it used to carry was written by an UpdateItem the deployed
        # IAM policy denies and no reader consulted.
        budgets.mark_pending_failed_best_effort(tenant_id=tenant_id, sk=hold_sk)
        raise _err_402("tenant_pool_exhausted")
    # RESERVE_APPLIED or RESERVE_ALREADY: the debit is a fact. Continue.
    try:
        logger.info("ledger_transact_latency", op="external_authorize_reserve_pending",
                    duration_ms=round((time.perf_counter() - _t0) * 1000, 3),
                    tenant_id=tenant_id)
    except Exception:  # noqa: BLE001
        pass

    # STEP 3 (async, off the critical path): PENDING -> ACTIVE. If it loses to a
    # sweeper fence, the reconciler recovers the (committed) debit; we alert. On a
    # non-CCF transient the hold stays PENDING and capture's helping-CAS activates
    # it later, so we never fail the caller here.
    try:
        activated = budgets.hold_activate(tenant_id=tenant_id, sk=hold_sk)
        if not activated:
            logger.error("pending_activate_lost_to_fence", tenant_id=tenant_id,
                         period=period, hold_id=hold_id)
    except Exception:  # noqa: BLE001 — step 3 is best-effort; debit is already durable
        logger.warning("pending_activate_transient", tenant_id=tenant_id,
                       period=period, hold_id=hold_id)

    # Nothing to finalize: a replay is decided by the pool marker and the hold's
    # own status, so the intent row is written once and never touched again.
    return ExternalAuthorizeResult(
        authorization_id=authorization_id, hold_id=hold_id, hold_sk=hold_sk,
        period=period, amount_microusd=amount, expires_at_epoch=hold_expires_at,
        capture_mode=capture_mode, replayed=(outcome == budgets.RESERVE_ALREADY),
    )


@dataclass
class ExternalAuthorizeResult:
    """Outcome of `reserve_external_authorization` — the addressing + amounts the
    authorize endpoint needs for its response. `replayed=True` when a duplicate
    Idempotency-Key returned the ORIGINAL authorization (endpoint answers 200,
    not 201)."""

    authorization_id: str
    hold_id: str
    hold_sk: str
    period: str
    amount_microusd: int
    expires_at_epoch: int
    capture_mode: str
    replayed: bool


class ExternalAuthorizeNoPool(Exception):
    """The tenant has no pool budget for the period, so an external authorize has
    nothing to reserve against. The endpoint maps this to 404 (a tenant without a
    pool is indistinguishable, to an external caller, from an unconfigured one)."""

    def __init__(self, tenant_id: str, period: str):
        super().__init__(f"tenant {tenant_id} has no pool for {period}")
        self.tenant_id = tenant_id
        self.period = period


def _read_idemp_with_prev_period(ledger, tenant_id, period, idempotency_key):
    """Read the IDEMP row for a key, then for the previous period.

    `get_idemp` now looks in the period-independent partition first, so the key's
    identity no longer expires with the period and this second call covers only
    rows written BEFORE that change (they sit in the money partition, and a retry
    across a boundary would otherwise miss them). Kept for exactly that window;
    both reads are ConsistentRead."""
    row = ledger.get_idemp(
        tenant_id=tenant_id, period=period, idempotency_key=idempotency_key
    )
    if row is not None:
        return row
    return ledger.get_idemp(
        tenant_id=tenant_id,
        period=_previous_period(period),
        idempotency_key=idempotency_key,
    )


class IdempotencyKeyReuse(Exception):
    """The same Idempotency-Key was reused for a DIFFERENT request body (or two
    distinct keys collided under sanitization). The endpoint maps this to 422 —
    it must NEVER silently replay a mismatched authorization (Fable authcap
    review-1 H-1). Guards against both a client mixing up two requests and the
    _safe_idemp_token collision handing back the wrong hold."""


def _commit_evidence(budgets, ledger, *, tenant_id: str, period: str,
                     hold_id: str, hold_sk: str) -> Optional[str]:
    """What durable fact proves this reservation's debit committed, or None.

    "Committed" has four possible witnesses and they are not interchangeable — each
    protocol leaves a different one — so the question is asked as "is there ANY
    witness" rather than "is the witness I expect present":

      * `marker`   — the PENDING protocol's commit point writes the pool debit and a
                     fixed-size marker in one transaction, so the marker existing IS
                     the debit existing.
      * `hold`     — the hold has been activated, which only happens after the debit.
      * `terminal` — the reservation has an ending. Nothing can end a debit that
                     never happened, so a SETTLE/RELEASE/RECLAIM is a witness even
                     after every other trace has been cleaned up. This is the one
                     that matters for a capture retry arriving after the terminal
                     landed but before the asynchronous RESERVE projection did.
      * `reserve`  — the transactional path writes the RESERVE event inside the
                     reserve transaction.

    Read order is cheapest-and-most-local first; any hit is conclusive, so the later
    reads are skipped."""
    if hold_sk:
        try:
            if budgets.pool_marker_amount(
                    tenant_id=tenant_id, period=period, hold_id=hold_id) is not None:
                return "marker"
        except Exception:  # noqa: BLE001 — a marker read failure is not a verdict.
            pass
        hold = budgets.get_hold(tenant_id=tenant_id, sk=hold_sk)
        if hold is not None and str(hold.get("status", "")) == "ACTIVE":
            return "hold"
    if ledger.get_terminal(tenant_id=tenant_id, period=period, hold_id=hold_id):
        return "terminal"
    if ledger.get_reserve(tenant_id=tenant_id, period=period, hold_id=hold_id):
        return "reserve"
    return None


def _replay_committed_or_refuse(
    budgets, ledger, idemp_row: dict, request_fingerprint: str, *,
    tenant_id: str, idempotency_key: str,
) -> ExternalAuthorizeResult:
    """Resolve a duplicate Idempotency-Key by reading state, for either protocol.

    The identity checks come first and are unconditional: a key reused for a
    different body, or a key that does not match the row it addressed, is a 422 and
    never a replay — including when the debit did commit, because the caller would
    otherwise receive an authorization it did not ask for.

    Then the verdict, which is a read rather than an inference:

      committed        -> replay the ORIGINAL authorization from the row.
      hold FAILED      -> replay the original refusal (402). The attempt is over.
      hold PENDING     -> 503 with the same key: the original attempt has not
                          reached its commit point, and reporting success here is
                          the fail-open this function exists to prevent.
      nothing readable -> 404. The row addresses a reservation with no trace left.

    "Committed" is `_commit_evidence`, not the row's own presence."""
    _idemp_identity_or_raise(idemp_row, request_fingerprint,
                             idempotency_key=idempotency_key)
    hold_id = str(idemp_row.get("hold_id", ""))
    hold_sk = str(idemp_row.get("hold_sk", ""))
    period = str(idemp_row.get("period", ""))
    if not hold_id or not period:
        # A row that cannot say which reservation it addresses cannot be replayed
        # into one. Refused for the same reason a row with no fingerprint is.
        raise IdempotencyKeyReuse(
            "the stored authorization does not name a reservation")

    if _commit_evidence(budgets, ledger, tenant_id=tenant_id, period=period,
                        hold_id=hold_id, hold_sk=hold_sk) is not None:
        return _idemp_result(idemp_row)

    hold = budgets.get_hold(tenant_id=tenant_id, sk=hold_sk) if hold_sk else None
    status = str((hold or {}).get("status", "")) if hold else ""
    if status == "FAILED":
        raise _err_402("tenant_pool_exhausted")
    if status == "PENDING":
        raise HTTPException(status_code=503, detail={
            "type": "budget_unavailable", "reason": "pool_reservation_in_flight",
            "message": "A reservation for this Idempotency-Key is in flight. "
                       "Retry shortly."})
    raise HTTPException(status_code=404, detail="authorization not found")


def _idemp_identity_or_raise(
    idemp_row: dict, request_fingerprint: str, *, idempotency_key: str
) -> None:
    """Reconstruct an ExternalAuthorizeResult from a stored IDEMP row (a
    duplicate-key replay). The row froze everything the authorize response needs,
    so a replay is a pure read — no rehydrate, no second reserve.

    First it verifies the incoming request's fingerprint matches the stored one:
    a mismatch means the key was reused for a different request (or a sanitize
    collision), which must be a 422, never a wrong-authorization replay (H-1).
    A MISSING stored fingerprint is also a mismatch (Fable authcap review-4 M-C):
    every IDEMP row this code writes carries one, so an absent fingerprint means
    a partial write / hand-inserted / foreign row — replaying it could hand back
    an authorization for a different body, so reject rather than skip the check.

    It then verifies the KEY itself. The row is addressed by a digest, which is
    collision-free, but the pre-digest sanitised address was not: two distinct keys
    could resolve to one row, and a fingerprint match would then let the second key
    replay the first one's authorization. The raw key is stored for exactly this
    check, so a replay proves the row belongs to THIS key rather than trusting the
    address it was found at. A row with no stored key is refused for the same
    reason the missing fingerprint is."""
    stored_fp = str(idemp_row.get("request_fingerprint", ""))
    if stored_fp != request_fingerprint:
        raise IdempotencyKeyReuse(
            "Idempotency-Key reused for a different request"
        )
    stored_key = idemp_row.get("idempotency_key")
    if stored_key is None or str(stored_key) != str(idempotency_key):
        raise IdempotencyKeyReuse(
            "Idempotency-Key does not match the stored authorization's key"
        )
    return None


def _idemp_result(idemp_row: dict) -> ExternalAuthorizeResult:
    """The authorize response a stored IDEMP row froze, replayed verbatim.

    Separate from the identity checks because the two answer different questions —
    "may this caller have this row" and "what did this row say" — and a resolver
    that has to decide committed-or-not in between needs them apart."""
    return ExternalAuthorizeResult(
        authorization_id=str(idemp_row["authorization_id"]),
        hold_id=str(idemp_row["hold_id"]),
        hold_sk=str(idemp_row["hold_sk"]),
        period=str(idemp_row["period"]),
        amount_microusd=int(idemp_row["amount_microusd"]),
        expires_at_epoch=int(idemp_row["expires_at"]),
        capture_mode=str(idemp_row.get("capture_mode", "amount")),
        replayed=True,
    )


def _rehydrate_from_hold(
    tenant_id: str, period: str, hold_id: str, hold_sk: str,
    hold: dict, pool_reserved: int,
) -> "ReservationContext":
    """Build the ReservationContext from the enriched HOLD row ALONE (two-item
    migration). Byte-equivalent to the RESERVE-event path: same field shape, same
    run-attribution rules, same rate_snapshot rehydration — so settle/void run
    identically. Caller has already applied the C-1 gate (hold.source=="external").
    """
    rate_snap = None
    pricing_key = None
    raw = hold.get("rate_snapshot")
    if raw:
        try:
            import json as _json

            from .pricing import RateSnapshot as _RS

            rate_snap = _RS.from_ledger_dict(_json.loads(raw))
            pricing_key = rate_snap.pricing_key
        except Exception:  # noqa: BLE001 — a corrupt snapshot degrades to amount-mode.
            rate_snap = None
    # Same fallback rule as the RESERVE-event path: only restore workflow_run_id
    # when the reserve was NOT a hold_id fallback (else a synthetic hold_id would
    # resurface as a real run on settle).
    restored_run_id = None
    if hold.get("run_id_source") != "hold_id_fallback":
        _rid = hold.get("run_id")
        restored_run_id = str(_rid) if _rid else None
    return ReservationContext(
        tenants_repo=UserTenantsRepository(),
        reservation_tokens=0,
        pool_reserved_microusd=pool_reserved,
        period=period,
        pricing_key=pricing_key,
        rate_snapshot=rate_snap,
        tenant_id=tenant_id,
        pool_active=True,
        hold_id=hold_id,
        hold_sk=hold_sk,
        workflow_run_id=restored_run_id,
        source="external",
    )


def _dual_read_crosscheck(tenant_id: str, period: str, hold_id: str, hold: dict) -> None:
    """Migration guard (dual-read phase): compare the enriched HOLD against the
    still-synchronous RESERVE event.

    H-A (Fable authcap review-4) is MONEY-SAFETY and is preserved across the
    migration: while both durable sources exist, if the HOLD's `amount_microusd`
    and the RESERVE event's `reserved_delta_microusd` DISAGREE, settling would move
    `pool_reserved` by an amount the ledger's +reserved never recorded — breaking
    I2 silently. So an AMOUNT mismatch RAISES `ExternalHoldInconsistent` (surfaced
    as 409), exactly as the legacy RESERVE-event path did; this is the reason the
    HOLD-only path keeps reading the RESERVE event during step 3. Once step 4
    removes the synchronous RESERVE event there is a single source and nothing to
    diverge, so this guard (and the crosscheck) is retired with it.

    A source/run_id divergence is NOT money-critical (it would change auth/attrib,
    not the amount moved) — it is logged as cutover-readiness telemetry, not
    raised, so it never fails a live capture/void."""
    try:
        reserve_evt = _reaper_ledger().get_reserve(
            tenant_id=tenant_id, period=period, hold_id=hold_id
        )
    except Exception:  # noqa: BLE001 — a lookup error is best-effort telemetry.
        return
    if reserve_evt is None:
        logger.warning(
            "hold_dualread_reserve_missing",
            tenant_id=tenant_id, period=period, hold_id=hold_id,
        )
        return
    # Money-critical: refuse to settle on an amount divergence (H-A).
    h_amt = int(hold.get("amount_microusd", 0))
    r_amt = int(reserve_evt.get("reserved_delta_microusd", 0))
    if h_amt != r_amt:
        logger.error(
            "external_hold_amount_mismatch",
            tenant_id=tenant_id, period=period, hold_id=hold_id,
            hold_amount=h_amt, reserve_delta=r_amt,
        )
        raise ExternalHoldInconsistent(hold_id)
    # Informational: non-money divergences inform the cutover go/no-go, never fail.
    mismatches = {}
    if hold.get("source") != reserve_evt.get("source"):
        mismatches["source"] = (hold.get("source"), reserve_evt.get("source"))
    if hold.get("run_id") != reserve_evt.get("run_id"):
        mismatches["run_id"] = (hold.get("run_id"), reserve_evt.get("run_id"))
    if mismatches:
        logger.warning(
            "hold_dualread_mismatch",
            tenant_id=tenant_id, period=period, hold_id=hold_id,
            mismatches=mismatches,
        )


def rehydrate_reservation_context(
    *,
    tenant_id: str,
    period: str,
    hold_id: str,
    hold_sk: str,
) -> Optional[ReservationContext]:
    """Rebuild the ReservationContext for an external hold from the ledger, so a
    capture/void in a SEPARATE HTTP call runs `_settle_pool_side`/`release_pool`
    BYTE-IDENTICALLY to the in-memory path (Fable authcap B — money logic is not
    forked; only the ctx's construction is).

    Source of truth is the RESERVE ledger event (durable, carries the frozen
    rate_snapshot + source) plus the HOLD row (existence + amount). Returns None
    when the hold row is gone — the caller then reads the terminal to answer
    captured/voided/expired (it must NOT fabricate a context and settle a
    non-existent hold). The returned context has `pool_active=True`,
    `source="external"`, and the SAME field shape a fresh reserve produced, so
    the F-1 equivalence property can assert the two produce identical txn items.

    SECURITY (Fable authcap review-1 C-1): the RESERVE event's `source` MUST be
    "external". The authorization token is not tamper-proof (by design — the PK
    is always the authed tenant), and an inline LLM hold shares the SAME table +
    sk shape, with hold_id/period/expiry all discoverable from the tenant's own
    billing:read surface. So without this gate a tenant could forge a token
    pointing at its OWN inline hold and void/capture it — erasing real spend
    (a reserved-return with no charge) or pre-empting the inline settle. Gating
    rehydrate on source=="external" makes external capture/void reach ONLY holds
    that the external authorize API itself created. A non-external (or absent)
    RESERVE → None → the endpoint answers 404, exactly as for a bogus token.
    """
    budgets = TenantBudgetsRepository()
    hold = budgets.get_hold(tenant_id=tenant_id, sk=hold_sk)

    # Two-item migration step 2/3 (docs/design/ledger-hot-path.md): the HOLD row
    # now carries `source`/`rate_snapshot`/description synchronously, so the C-1
    # gate and rehydration can read the HOLD ALONE — no dependency on the RESERVE
    # event, which is moving to an async Streams projection.
    #
    # PATH SELECTION (Fable review-2 finding 2): a hold takes the HOLD-only path
    # iff it is post-enrichment. Two independent signals both route to HOLD-only,
    # and neither can misroute a pre-enrichment hold there:
    #   * the hold already carries `source` (it was written by enriched code), OR
    #   * the hold was minted AT OR AFTER _ENRICHMENT_EPOCH (created_at gate).
    # A pre-epoch hold with no `source` falls to the legacy RESERVE-event path,
    # which still exists during step 3 (write side is still 4-item). The epoch
    # gate is what makes the eventual fallback DELETION safe: after epoch + max
    # hold TTL, no pre-epoch hold can still be live. C-1 stays fail-closed on the
    # HOLD-only path: a missing/non-external source denies (404).
    hold_has_source = hold is not None and "source" in hold
    hold_is_post_epoch = (
        _ENRICHMENT_EPOCH is not None
        and hold is not None
        and (_ce := _hold_created_epoch(hold)) is not None
        and _ce >= _ENRICHMENT_EPOCH
    )
    if hold_has_source or hold_is_post_epoch:
        if hold is None or hold.get("source") != "external":
            return None
        pool_reserved = int(hold.get("amount_microusd", 0))
        # PENDING protocol (docs/design/pending-protocol.md, Fable helping-CAS):
        # a capture/void may arrive while the hold is still PENDING (step 3 async
        # activate lost/delayed). settle is ACTIVE-only, so HELP it forward — but
        # ONLY after confirming the debit committed via the pool marker (A1). No
        # marker = the debit did not commit (ambiguous/uncommitted) → deny (404),
        # never settle an un-debited hold (which would credit-back an amount the
        # pool never lost = oversell).
        if str(hold.get("status", "")) == "PENDING":
            budgets_repo = TenantBudgetsRepository()
            marker = budgets_repo.pool_marker_amount(
                tenant_id=tenant_id, period=period, hold_id=hold_id)
            if marker is None:
                return None  # debit not committed yet — not capturable
            # Help the hold to ACTIVE (idempotent CAS; a racing activate/fence is
            # resolved by single-item serialization). If a fence already won, the
            # reconciler credits the marker back and this capture 404s — leak-safe.
            if not budgets_repo.hold_activate(tenant_id=tenant_id, sk=hold_sk):
                if str((budgets_repo.get_hold(tenant_id=tenant_id, sk=hold_sk) or {}
                        ).get("status", "")) != "ACTIVE":
                    return None
        # Cross-check against the synchronous RESERVE event ONLY when one exists
        # (transaction mode / migration). The PENDING protocol writes no RESERVE
        # event, so skip the crosscheck for a pending-origin hold to avoid a
        # spurious "reserve missing" warning on every capture.
        if _reserve_protocol_for(tenant_id) != "pending":
            _dual_read_crosscheck(tenant_id, period, hold_id, hold)
        return _rehydrate_from_hold(tenant_id, period, hold_id, hold_sk, hold,
                                    pool_reserved)

    # Legacy path: no enrichment on the HOLD → the RESERVE event is the only
    # source of source/amount/rate_snapshot. (Removed after all pre-enrichment
    # holds' max TTL elapses.)
    ledger = _reaper_ledger()
    reserve_evt = ledger.get_reserve(
        tenant_id=tenant_id, period=period, hold_id=hold_id
    )
    # C-1 gate: only holds minted by the external authorize API are rehydratable.
    if reserve_evt is None or reserve_evt.get("source") != "external":
        return None

    if hold is None:
        return None
    pool_reserved = int(hold.get("amount_microusd", 0))

    # H-A (Fable authcap review-4): the authorized amount has two durable sources
    # — the HOLD row's amount_microusd (which settle SUBTRACTS from pool_reserved
    # and the capture 422 guard compares against) and the RESERVE event's
    # reserved_delta_microusd (what the +reserved side recorded, and what GET
    # displays). In the normal single-txn reserve they are equal. If they DIVERGE
    # (a repair script, a future amount-adjust feature, or corruption edited only
    # one), settling would move pool_reserved by an amount the ledger's +reserved
    # never recorded — breaking I2 (ledger-derivability) silently. Refuse to
    # settle a hold whose two amounts disagree: raise (the endpoint's outer
    # handling surfaces it) rather than move money on an inconsistent hold.
    reserve_delta = int(reserve_evt.get("reserved_delta_microusd", 0))
    if pool_reserved != reserve_delta:
        logger.error(
            "external_hold_amount_mismatch",
            tenant_id=tenant_id, period=period, hold_id=hold_id,
            hold_amount=pool_reserved, reserve_delta=reserve_delta,
        )
        raise ExternalHoldInconsistent(hold_id)

    rate_snap = None
    pricing_key = None
    raw = reserve_evt.get("rate_snapshot")
    if raw:
        try:
            import json as _json

            from .pricing import RateSnapshot as _RS

            rate_snap = _RS.from_ledger_dict(_json.loads(raw))
            pricing_key = rate_snap.pricing_key
        except Exception:  # noqa: BLE001 — a corrupt snapshot degrades to amount-mode.
            rate_snap = None
    # M-A (Fable authcap review-4): restore the run attribution from the RESERVE
    # event so the SETTLE keys the run-index the SAME way. Honour the fallback
    # marker: a hold reserved WITHOUT a real workflow_run_id stored run_id=hold_id
    # with run_id_source="hold_id_fallback" — feeding that hold_id back as
    # workflow_run_id would make settle write run_id_is_fallback=False and surface
    # a synthetic hold_id as a real run (the external analog of F1). So restore
    # workflow_run_id ONLY when the RESERVE was NOT a fallback.
    restored_run_id = None
    if reserve_evt.get("run_id_source") != "hold_id_fallback":
        _rid = reserve_evt.get("run_id")
        restored_run_id = str(_rid) if _rid else None
    return ReservationContext(
        tenants_repo=UserTenantsRepository(),
        reservation_tokens=0,
        pool_reserved_microusd=pool_reserved,
        period=period,
        pricing_key=pricing_key,
        rate_snapshot=rate_snap,
        tenant_id=tenant_id,
        pool_active=True,
        hold_id=hold_id,
        hold_sk=hold_sk,
        workflow_run_id=restored_run_id,
        source="external",
    )


def _reserve_quota_without_pool(
    user,
    reservation_tokens: int,
    *,
    repo,
    period: str,
    pricing_key: Optional[str],
    quota_lines: list,
    quota_model: Optional[str],
    selected_model: Optional[str],
    quota_reserved_amount: int,
    bound_microusd: Optional[int] = None,
) -> ReservationContext:
    """Reserve per-user tokens AND a per-model quota atomically, with NO pool.

    For tenants that configure a per-model quota but no dollar pool. Same
    snapshot-optimistic retry as the pooled path, but the transaction is just
    [user_txn, *quota_lines] — no pool debit, no HOLD row. A quota
    ConditionalCheckFailed (index >= 1) means the quota is exhausted → raise
    QuotaExhausted so the caller's cascade advances; a user-row CCF (index 0) is
    the retryable snapshot race. Fails closed: a quota-configured request must
    never slip through unmetered (the Fable F-3 hole).

    `bound_microusd`, when it differs from `quota_reserved_amount`, is the
    sound bound to RECORD on `measured_bound_microusd` — see `reserve_credit`'s
    identically-named parameter. In practice this path never sees a genuine
    divergence (`shadow_mode` only applies when a pool row exists, and this
    function is only reached when one does not), but the `pool_vanished`
    caller in `reserve_credit` passes it through anyway rather than assume
    that invariant holds forever.
    """
    client = _low_level_client()
    saw_throttle = False
    for _attempt in range(_RESERVE_MAX_RETRIES):
        if _attempt:
            time.sleep(_contention_backoff(_attempt))
        item = repo.get(user.user_id, user.org_id, consistent_read=True)
        if not item:
            raise _err_402("personal_budget_exhausted")
        total = int(item.get("total_credit", 0))
        used = int(item.get("credit_used", 0))
        if used + reservation_tokens > total:
            logger.info("credit_exhausted_402", user_id=user.user_id,
                        tenant_id=user.org_id, reason="personal_budget_exhausted")
            raise _err_402("personal_budget_exhausted")

        user_txn = repo.reserve_txn_item(
            user_id=user.user_id, tenant_id=user.org_id,
            tokens=reservation_tokens, expected_total=total,
        )
        txn_items = [user_txn, *quota_lines]
        try:
            client.transact_write_items(
                TransactItems=txn_items,
                ClientRequestToken=_fresh_idempotency_token(),
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code", "") != "TransactionCanceledException":
                raise
            reasons = e.response.get("CancellationReasons", []) or []
            # Quota lines start at index 1 here (index 0 is the user row). A
            # ConditionalCheckFailed on any quota line = quota exhausted.
            if quota_model is not None and len(reasons) > 1:
                for r in reasons[1:]:
                    if r.get("Code", "") == "ConditionalCheckFailed":
                        logger.info("model_quota_exhausted", tenant_id=user.org_id,
                                    model=quota_model, period=period)
                        raise QuotaExhausted(quota_model)
            codes = {r.get("Code", "") for r in reasons}
            if codes & {"ThrottlingError", "ProvisionedThroughputExceeded",
                        "TransactionConflict", "RequestLimitExceeded"}:
                saw_throttle = True
            continue

        return ReservationContext(
            tenants_repo=repo,
            reservation_tokens=reservation_tokens,
            period=period,
            pricing_key=pricing_key,
            tenant_id=user.org_id,
            pool_active=False,
            quota_lines=quota_lines,
            selected_model=selected_model,
            quota_reserved_amount=quota_reserved_amount,
            quota_user_id=user.user_id,
            quota_period=period,
            measured_bound_microusd=(
                int(bound_microusd) if bound_microusd is not None
                else (int(quota_reserved_amount) if quota_reserved_amount else None)
            ),
        )

    logger.warning("quota_reserve_retries_exhausted", user_id=user.user_id,
                   tenant_id=user.org_id, period=period, throttled=saw_throttle)
    if saw_throttle:
        raise HTTPException(status_code=503, detail={
            "type": "budget_unavailable", "reason": "quota_reservation_contended",
            "message": "Quota reservation is temporarily unavailable. Retry shortly."})
    # Lost every snapshot race for the user row — treat as personal budget.
    raise _err_402("personal_budget_exhausted")


def release_pool(context) -> None:
    """Release a pooled reservation on an error path (no billable usage).

    Safe to call with anything the route handlers hold as `tenants_repo`: a
    bare `UserTenantsRepository` (no pool) is ignored, a `ReservationContext`
    releases its outstanding pool hold exactly once. This is the pool-side
    counterpart to the token-side `refund()` the error paths already call.
    """
    releaser = getattr(context, "release_pool", None)
    if callable(releaser):
        releaser()
    # Release the per-model quota reservation too (invoke failed, no spend), so
    # a failed attempt doesn't leak `used` until period rollover.
    _release_quota_for(context)


def _quota_period(context) -> Optional[str]:
    """The period the quota was RESERVED against — never a fresh current_period().

    Settling/releasing against `current_period()` at settle time (Fable F-1)
    would hit the WRONG month's row for any request that crossed midnight
    between reserve and settle: the reserved period leaks (never released) and
    the new period is negative-seeded (over-admits). A missing value is treated
    as "no known reserved period" → the caller no-ops rather than guessing.
    """
    return getattr(context, "quota_period", None)


def _release_quota_for(context) -> None:
    model = getattr(context, "selected_model", None)
    amt = int(getattr(context, "quota_reserved_amount", 0) or 0)
    period = _quota_period(context)
    if not model or amt <= 0 or not period:
        return
    try:
        from .routing import quota as _quota
        _quota.release_quota(
            tenant_id=getattr(context, "tenant_id", ""),
            user_id=getattr(context, "quota_user_id", None),
            model=model,
            period=period,
            reserved_amount=amt,
        )
    except Exception:  # noqa: BLE001 — quota release must never fail the request
        logger.warning("quota_release_failed", model=model, exc_info=True)
    finally:
        # Idempotent: a second release/settle on the same context is a no-op
        # (Fable F-6), so no double -reserved can drive `used` negative.
        context.quota_reserved_amount = 0


def _settle_quota_for(context, actual_microusd: int) -> None:
    model = getattr(context, "selected_model", None)
    reserved = int(getattr(context, "quota_reserved_amount", 0) or 0)
    period = _quota_period(context)
    if not model or reserved <= 0 or not period:
        return
    try:
        from .routing import quota as _quota
        _quota.settle_quota(
            tenant_id=getattr(context, "tenant_id", ""),
            user_id=getattr(context, "quota_user_id", None),
            model=model,
            period=period,
            reserved_amount=reserved,
            actual_amount=int(actual_microusd),
        )
    except Exception:  # noqa: BLE001 — quota settle must never fail the request
        logger.warning("quota_settle_failed", model=model, exc_info=True)
    finally:
        # Idempotent (Fable F-6): clear so a later release/double-settle no-ops.
        context.quota_reserved_amount = 0


def _settled_only_txn_item(*, table_name: str, tenant_id: str, period: str, actual_microusd: int):
    """Aggregate update that records spend WITHOUT touching `pool_reserved`.

    Used by the settle fallback when the reaper already reclaimed this
    reservation's hold (and thus already returned its reserved share): we must
    still record the actual spend, but decrementing `pool_reserved` again would
    double-subtract. Gated on `attribute_exists(tenant_id)` so a vanished pool
    row is a no-op.

    Headroom: the reaper's reclaim already returned the full reservation to
    headroom (`+= reserved`). Now that the true spend is known, deduct it —
    `headroom -= actual` — so the invariant `headroom == limit - reserved -
    settled` holds after this settled-only spend record.
    """
    from dynamo.tenant_budgets import budget_sk

    return {
        "Update": {
            "TableName": table_name,
            "Key": {"tenant_id": {"S": tenant_id}, "sk": {"S": budget_sk(period)}},
            "UpdateExpression": (
                "ADD pool_settled_microusd :actual, pool_headroom_microusd :dh"
            ),
            "ConditionExpression": "attribute_exists(tenant_id)",
            "ExpressionAttributeValues": {
                ":actual": {"N": str(int(actual_microusd))},
                ":dh": {"N": str(-int(actual_microusd))},
            },
        }
    }


def _cancellation_codes(e: ClientError) -> list:
    """Per-item CancellationReasons codes, index-aligned with the TransactItems."""
    return [r.get("Code", "") for r in (e.response.get("CancellationReasons", []) or [])]


def _reaper_ledger():
    """The credit ledger repo, imported lazily so the reaper (and the module's
    import graph) does not hard-depend on the ledger when it is not used."""
    from dynamo import CreditLedgerRepository

    return CreditLedgerRepository()


def _recover_spend_via_late_settle(
    *,
    client,
    ledger,
    budgets_table_name: str,
    tenant_id: str,
    period: str,
    hold_id: str,
    actual_microusd: int,
    run_id: str,
    run_is_fallback: bool,
    facts: dict,
) -> None:
    """Record spend that a settle would otherwise lose because the reaper
    reclaimed the hold first (Phase 2 revenue-leak fix).

    The reaper's RECLAIM already returned `reserved`, so this moves the settled
    side ONLY: a single TransactWriteItems of
      [0] pool settled-only counter (+actual, reserved untouched),
      [1] LATE_SETTLE ledger Put (distinct sk, attribute_not_exists),
      [2] ConditionCheck: the terminal really is a RECLAIM.
    Idempotent: a retry storm CCFs on [1]; we then read the existing LATE_SETTLE
    and treat a matching actual as success, a mismatch as a client bug (metric).
    """
    so_items = [
        _settled_only_txn_item(
            table_name=budgets_table_name,
            tenant_id=tenant_id,
            period=period,
            actual_microusd=actual_microusd,
        ),
        ledger.late_settle_txn_item(
            tenant_id=tenant_id,
            period=period,
            hold_id=hold_id,
            settled_delta_microusd=int(actual_microusd),
            run_id=run_id,
            run_id_is_fallback=run_is_fallback,
            span_id=facts.get("span_id"),
            request_id=facts.get("request_id"),
            group_id=facts.get("group_id"),
            model_id=facts.get("model_id"),
            pricing_version=facts.get("pricing_version"),
            pricing_key=facts.get("pricing_key"),
            # INV-R6: the SAME frozen rating the SETTLE path would have written
            # (computed from ctx.rate_snapshot), so SETTLE and this reaper-race
            # LATE_SETTLE record identical money.
            rating=facts.get("rating"),
            tokens_in=facts.get("tokens_in"),
            tokens_out=facts.get("tokens_out"),
        ),
        ledger.terminal_conditioncheck_is_reclaim(
            tenant_id=tenant_id, period=period, hold_id=hold_id
        ),
    ]
    # Idempotency comes from the LATE_SETTLE sk's `attribute_not_exists` (exactly
    # one LATE_SETTLE per hold), NOT from the ClientRequestToken — so a FRESH
    # token per attempt is correct. A derived/stable token would additionally
    # require byte-identical request payloads across retries, which the ledger
    # Put cannot promise (its ts_ms differs per attempt), and DynamoDB rejects a
    # token reused with a different payload (IdempotentParameterMismatch). The
    # fresh token still dedupes botocore's own transparent retry of THIS call.
    #
    # A TRANSIENT cancel (TransactionConflict / throttle) is RETRIED IN-PLACE with
    # backoff, mirroring the primary settle loop: settle runs at the streaming
    # tail with no client retry, and the reaper will not re-fire this (already
    # reclaimed) hold, so swallowing a transient here would permanently drop the
    # spend (the leak Phase 2 closes). On retry exhaustion we RAISE — NOT a silent
    # success. Honest note on recovery (Fable P2 review-2 R2-1): the recovery moves
    # counter[0] and ledger[1] ATOMICALLY, so if it never commits, counter and
    # ledger both miss the spend EQUALLY — reconciliation (counter−ledger drift)
    # therefore CANNOT see this gap. The only signal is the loud
    # `pool_settle_late_settle_retries_exhausted` / `pool_settle_failed` log
    # (alarmed in iac). Durable auto-redrive (an orphan sweep matching "RECLAIM
    # terminal with no LATE_SETTLE" against usage, or a pending-recovery outbox) is
    # future work — see the ledger Phase 2 task. The retry is safe because item [0]
    # is a bare `ADD` (no snapshot) and [1] is idempotent on its sk.
    transient = {
        "TransactionConflict",
        "ThrottlingError",
        "ThrottlingException",
        "ProvisionedThroughputExceeded",
        "RequestLimitExceeded",
    }
    for _attempt in range(_SETTLE_MAX_RETRIES):
        if _attempt:
            time.sleep(_contention_backoff(_attempt, cap=_SETTLE_BACKOFF_CAP_SECONDS))
        try:
            client.transact_write_items(
                TransactItems=so_items,
                ClientRequestToken=_fresh_idempotency_token(),
            )
            # docs/design/hard-ceiling.md section 8 ("a settle arriving after its
            # hold was reaped"): this settle books a charge with NO reservation
            # behind it (the reaper's RECLAIM already returned `reserved`) — a
            # LEGITIMATE, expected event under this design, not dropped and not
            # given a recreated reservation, but it must be BOOKED, TAGGED with
            # its own cause code, and ALARMED — not logged at info, which pages
            # no one. This is also the reason acceptance criterion 4
            # ("settled + reserved <= pool_limit") explicitly excludes events
            # marked reservation-less: this transaction adds to `settled` with
            # no corresponding `reserved` to release, by design.
            logger.error(
                "pool_settle_late_settle_recovered",
                tenant_id=tenant_id,
                period=period,
                hold_id=hold_id,
                actual_microusd=actual_microusd,
                cause="reservation_less",
            )
            return
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
            reasons = _cancellation_codes(e)
            # [1] = LATE_SETTLE Put. A CCF here means a LATE_SETTLE already exists
            # — a concurrent/retried recovery beat us. Read it and compare actual.
            late_dup = len(reasons) > 1 and reasons[1] == "ConditionalCheckFailed"
            if late_dup:
                existing = ledger.get_late_settle(
                    tenant_id=tenant_id, period=period, hold_id=hold_id
                )
                if existing is None:
                    # The Put CCF'd on attribute_not_exists, so a LATE_SETTLE MUST
                    # exist; a ConsistentRead that then finds none can only mean a
                    # defect (pk/sk mismatch between write and read). Do NOT return
                    # success — raise so it is not a silent drop (Fable P2 review-2
                    # R2-2: symmetric with the None-terminal handling in settle).
                    logger.error(
                        "pool_settle_late_settle_missing_after_ccf",
                        tenant_id=tenant_id,
                        period=period,
                        hold_id=hold_id,
                    )
                    raise
                existing_actual = int(existing.get("settled_delta_microusd", 0))
                if existing_actual == int(actual_microusd):
                    # Idempotent success: the spend is recorded exactly once.
                    return
                # First-writer-wins: a retry arrived with a DIFFERENT actual. Keep
                # the recorded value; surface the divergence (metric-filter alarm).
                logger.error(
                    "LateSettleActualMismatch",
                    tenant_id=tenant_id,
                    period=period,
                    hold_id=hold_id,
                    recorded_microusd=existing_actual,
                    attempted_microusd=int(actual_microusd),
                )
                return
            # Transient cancel → retry in-place (the settled-only item [0] is the
            # hot pool-counter row, so a conflict here is realistic).
            if any(code in transient for code in reasons):
                logger.warning(
                    "pool_settle_late_settle_transient_retry",
                    tenant_id=tenant_id,
                    period=period,
                    hold_id=hold_id,
                    attempt=_attempt,
                    reasons=reasons,
                )
                continue
            # [0] pool row vanished (legitimately deleted → nothing to reconcile)
            # WITHOUT a [2] ConditionCheck failure → benign no-op.
            pool_row_ccf = (
                len(reasons) > 0
                and reasons[0] == "ConditionalCheckFailed"
                and not (len(reasons) > 2 and reasons[2] == "ConditionalCheckFailed")
            )
            if pool_row_ccf:
                logger.info(
                    "pool_settle_late_settle_pool_vanished",
                    tenant_id=tenant_id,
                    period=period,
                    hold_id=hold_id,
                )
                return
            # [2] terminal-is-RECLAIM ConditionCheck failed: the terminal is
            # immutable+append-only, so a route that read RECLAIM cannot see it
            # flip — this signals a routing/consistency defect. Do not swallow.
            logger.error(
                "pool_settle_late_settle_unexpected_cancel",
                tenant_id=tenant_id,
                period=period,
                hold_id=hold_id,
                reasons=reasons,
            )
            raise
    # Transient retries exhausted: RAISE so the outer settle logs
    # pool_settle_failed and reconciliation catches the (settled-side) gap —
    # never report a silent success.
    logger.error(
        "pool_settle_late_settle_retries_exhausted",
        tenant_id=tenant_id,
        period=period,
        hold_id=hold_id,
        actual_microusd=actual_microusd,
    )
    raise RuntimeError(f"late-settle recovery exhausted retries for hold {hold_id}")


def _reported_count(v: Optional[int]) -> Optional[int]:
    """A reported token count, clamped at zero — or None when nothing was reported.

    `max(v, 0)` cannot be used directly any more: `None` is now a distinct value
    meaning "the provider did not report this leg", and it must survive to the
    rating record rather than being coerced into a measured zero.
    """
    if v is None:
        return None
    return max(int(v), 0)


def settle_reservation_and_log(
    *,
    user,
    tenants_repo,
    reservation: int,
    actual_input_tokens: int,
    actual_output_tokens: int,
    model_id: str,
    context: Optional[ReservationContext] = None,
    actual_cost_microusd: Optional[int] = None,
    # `None` means the provider did not report that leg — distinct from a reported
    # zero, and carried through to the rating record so the ledger does not assert a
    # measurement nobody made (see `mvp.pricing.rate_usage`).
    actual_cache_read_tokens: Optional[int] = None,
    actual_cache_write_tokens: Optional[int] = None,
    requested_model: Optional[str] = None,
    request_id: Optional[str] = None,
) -> None:
    """Settle the reservation against actual usage and write a UsageLogs row.

    Token side (always): refund the diff when actual <= reservation, or
    best-effort top-up + clamp with a `credit_overrun` warning when actual >
    reservation. UsageLogs always receives the true actual usage.

    Pool side (only when the reservation was pooled): move the reserved
    micro-USD out of `pool_reserved` and the actual micro-USD into
    `pool_settled` in one update, so the pool's outstanding reservation is
    released and real spend is recorded. The auto-derived cost prices cache
    read/write tokens too (`actual_cache_*`), so cached traffic is not billed at
    zero. The update carries a ClientRequestToken and is retried, and the
    context is marked finalized so a defensive double-settle cannot drive
    `pool_reserved` negative.

    `tenants_repo` is accepted positionally for backward compatibility;
    `context` (returned by reserve_credit) drives the pool settlement.
    """
    actual = max(actual_input_tokens + actual_output_tokens, 0)

    # ----- token side (unchanged semantics) -----
    diff = reservation - actual
    if diff > 0:
        tenants_repo.refund(
            user_id=user.user_id, tenant_id=user.org_id, tokens=diff
        )
    elif diff < 0:
        overrun = -diff
        try:
            tenants_repo.reserve(
                user_id=user.user_id,
                tenant_id=user.org_id,
                tokens=overrun,
            )
        except CreditExhaustedError:
            item = tenants_repo.get(user.user_id, user.org_id)
            clamped_gap = 0
            uncovered = overrun
            if item is not None:
                total_credit = int(item.get("total_credit", 0))
                used = int(item.get("credit_used", 0))
                clamped_gap = max(total_credit - used, 0)
                if clamped_gap > 0:
                    try:
                        tenants_repo.reserve(
                            user_id=user.user_id,
                            tenant_id=user.org_id,
                            tokens=clamped_gap,
                        )
                        uncovered = overrun - clamped_gap
                    except CreditExhaustedError:
                        clamped_gap = 0
            logger.warning(
                "credit_overrun",
                user_id=user.user_id,
                tenant_id=user.org_id,
                model_id=model_id,
                reservation=reservation,
                actual=actual,
                overrun=overrun,
                clamped=clamped_gap,
                uncovered=uncovered,
            )

    # ----- pool side (only when the reservation was pooled) -----
    # When the caller didn't pass an explicit actual cost, derive it from the
    # real usage. Layer 5: rate against the rate FROZEN at reserve time (a pure
    # function, no live-table read) so a rate flip between reserve and settle
    # cannot change the price. `_rating` is the frozen breakdown embedded on the
    # ledger terminal; its total IS the settled amount (single source of truth).
    from .pricing import UNVERSIONED_SENTINEL

    _rating = None
    if (
        actual_cost_microusd is None
        and context is not None
        and context.pool_active
        and context.pricing_key
    ):
        if context.rate_snapshot is not None:
            from .pricing import rate_usage

            _rating = rate_usage(
                context.rate_snapshot,
                input_tokens=actual_input_tokens,
                output_tokens=actual_output_tokens,
                cache_read_tokens=_reported_count(actual_cache_read_tokens),
                cache_write_tokens=_reported_count(actual_cache_write_tokens),
            )
            actual_cost_microusd = _rating.total_cost_microusd
        else:
            # A reservation admitted by THIS version always carries the rate it was
            # admitted at, because pricing fails closed. Reaching here means the
            # reservation predates that: a RESERVE event written by the previous
            # version during a rate-table failure, or before snapshots existed,
            # restored from the ledger after a deploy. Charging it from the live table
            # is the C2.2 violation this branch removed — and refusing is worse,
            # because nothing re-drives a failed settle, so the reaper would end it as
            # a reclaim with a settled delta of zero and the usage the gateway DID
            # observe would leave the ledger entirely.
            #
            # It settles at the amount the admission already debited. That is an upper
            # bound on any charge this request could have had (the pool was gated on
            # it), it needs no rate read at all, and the terminal carries the honest
            # `unversioned-legacy` label because no version priced it.
            logger.warning(
                "settle_without_frozen_rate_charging_reserved",
                pricing_key=context.pricing_key,
                reserved_microusd=int(context.pool_reserved_microusd or 0),
            )
            actual_cost_microusd = int(context.pool_reserved_microusd or 0)

    if (
        context is not None
        and context.pool_active
        and not context._pool_finalized
        and context.pool_reserved_microusd > 0
        and actual_cost_microusd is not None
        and context.period is not None
    ):
        # Finalize exactly once: releasing the hold and recording spend happen
        # together, and a defensive double-settle (e.g. error handler + the
        # streaming `finally`) must not double-subtract pool_reserved.
        context._pool_finalized = True
        # Hard-ceiling overrun record (docs/design/hard-ceiling.md item 4). Under a
        # sound bound this is always zero — an overrun is a defect report about
        # the bound, not an operating mode, so it is computed unconditionally
        # (never gated on bound_mode) and alarmed below whenever it fires on a
        # reservation that WAS bound-priced (bound_mode is not None). A
        # legacy-heuristic reservation (bound_mode is None) can still overrun —
        # that was always tolerated — so it is recorded but not alarmed as a
        # bound defect. `context.bound_mode` is None in `shadow_mode` (see
        # `_stamp_bound_metadata`) precisely so THIS reservation — legacy-
        # priced, even though the sound bound was separately computed and
        # recorded on `measured_bound_microusd` — is treated exactly like the
        # legacy-heuristic case here: recorded, never alarmed. Diffing against
        # the bound instead of `reserved_microusd` would compare two
        # differently-sourced numbers and break the ledger's own documented
        # invariant that `overrun_microusd == max(0, actual - reserved)` (see
        # `dynamo.credit_ledger`), so this stays keyed on the REAL admission
        # reservation, unchanged from before shadow mode existed.
        _reserved_microusd = int(context.pool_reserved_microusd)
        _overrun_microusd = max(0, int(actual_cost_microusd) - _reserved_microusd)
        _reserve_pricing_version = (
            context.rate_snapshot.version if context.rate_snapshot is not None else None
        )
        _estimate_inputs = None
        if context.bound_mode is not None:
            _estimate_inputs = {
                "input_bytes": context.reserved_input_bytes,
                "payload_hash": context.reserved_payload_hash,
                "extra_input_tokens": context.reserved_extra_input_tokens,
                "max_output_tokens": context.reserved_max_output_tokens,
                "effort_multiplier": context.reserved_effort_multiplier,
            }
            if _overrun_microusd > 0:
                # Cause code (contract section 9): distinguish the provider
                # not respecting the requested output ceiling — the ONE place
                # the provider's cooperation with `max_output_tokens *
                # effort_multiplier` (section 4's stated assumption) is
                # actually checked rather than assumed — from every other
                # overrun, which is a defect in the bound itself. (A third
                # cause, "no reservation found" / reservation-less, applies
                # to the LATE_SETTLE reaper-race path, not here — see
                # `_recover_spend_via_late_settle`.)
                _output_ceiling = None
                if context.reserved_max_output_tokens is not None:
                    _output_ceiling = (
                        int(context.reserved_max_output_tokens)
                        * int(context.reserved_effort_multiplier)
                    )
                _output_ceiling_violated = (
                    _output_ceiling is not None
                    and int(actual_output_tokens) > _output_ceiling
                )
                _cause = (
                    "output_ceiling_exceeded" if _output_ceiling_violated
                    else "bound_exceeded"
                )
                # Fail-closed alarm, not a warning (contract section 9): a
                # sound bound is NEVER exceeded in strict mode (section 6's
                # guarantee, modulo the reservation-less exception section 5
                # names explicitly — which this is not, since a live
                # ReservationContext exists here), so this line means either
                # the bound in force for this tenant/model is wrong, or the
                # provider did not respect the output ceiling it was given —
                # `cause` tells the two apart.
                logger.error(
                    "reservation_bound_overrun",
                    tenant_id=user.org_id,
                    period=context.period,
                    hold_id=context.hold_id,
                    model_id=model_id,
                    bound_mode=context.bound_mode,
                    cause=_cause,
                    reserved_microusd=_reserved_microusd,
                    actual_microusd=int(actual_cost_microusd),
                    overrun_microusd=_overrun_microusd,
                    reserve_pricing_version=_reserve_pricing_version,
                    output_ceiling_tokens=_output_ceiling,
                    actual_output_tokens=int(actual_output_tokens),
                )
            # Calibrated mode (docs/design/calibrated-mode.md) is explicitly OUT
            # OF SCOPE for this change and is not reachable today —
            # `dynamo.tenants.VALID_BOUND_MODES` only accepts "strict", so
            # `resolve_bound_mode` can never return "calibrated" for a real
            # tenant. This block is therefore dead code on every path this
            # change ships; it is kept (not deleted) so phase 2 does not have
            # to rediscover this wiring, and it stays provably inert because
            # the condition below can never be true.
            from dynamo.tenants import BOUND_MODE_CALIBRATED

            if (
                context.bound_mode == BOUND_MODE_CALIBRATED
                and context.reserved_input_bytes is not None
            ):
                from .reservation_bound import (
                    calibration_breached,
                    realized_tokens_per_byte,
                )

                _realized_input_tokens = (
                    int(actual_input_tokens)
                    + max(actual_cache_read_tokens or 0, 0)
                    + max(actual_cache_write_tokens or 0, 0)
                )
                _ratio = realized_tokens_per_byte(
                    _realized_input_tokens, context.reserved_input_bytes
                )
                if calibration_breached(context.pricing_key or "default", _ratio):
                    # Fail-closed per the contract: "a single settle above the
                    # calibration is a fail-closed event, not a warning" — this
                    # settle already happened (settle is unconditional and must
                    # stay that way), so "fail-closed" here means loudly, at
                    # error level, with everything needed to act on it, not
                    # silently degrading the mode for the next request.
                    logger.error(
                        "calibration_breach",
                        tenant_id=user.org_id,
                        pricing_key=context.pricing_key,
                        realized_tokens_per_byte=_ratio,
                        model_id=model_id,
                        hold_id=context.hold_id,
                    )
        try:
            _settle_pool_side(
                user,
                context,
                int(actual_cost_microusd),
                ledger_facts={
                    "tokens_in": int(actual_input_tokens),
                    "tokens_out": int(actual_output_tokens),
                    "model_id": model_id,
                    # BUG #1 FIX + Fable review H3/M1: pricing_version labels the
                    # terminal with the VERSION the charge was actually computed
                    # at. It is set ONLY when we produced a frozen-snapshot rating
                    # for THIS settle (`_rating is not None`). When the charge did
                    # NOT go through the snapshot — an explicit caller-supplied
                    # cost, or an unpriced reservation — we must NOT stamp a
                    # version the amount was not derived from (that would be a
                    # false dispute label AND relapse bug#1 by writing the
                    # pricing_key). The honest sentinel is the only remaining
                    # case: a reservation that carried no rate at all. There is no
                    # longer a `snapshot-failed` case to distinguish, because a
                    # rate that cannot be frozen now refuses the request instead of
                    # charging from a live read later.
                    "pricing_version": (
                        _rating.pricing_version
                        if _rating is not None
                        else UNVERSIONED_SENTINEL
                    ),
                    "pricing_key": context.pricing_key,
                    "rating": _rating.to_ledger_dict() if _rating is not None else None,
                    "settle_reason": "completion",
                    # Attribution → the ledger event's run-index key. When the
                    # client supplied a workflow_run_id, the terminal's gsi1pk is
                    # TENANT#<id>#RUN#<workflow_run_id>, so per-run billing
                    # (GET /billing/runs/<workflow_run_id>) finds it. Absent →
                    # _settle_pool_side falls back to hold_id (run_id_is_fallback).
                    #
                    # NOTE (Fable L5d-e review F1): deliberately NOT passing
                    # request_id here. _settle_pool_side's run_id chain is
                    # `run_id or request_id or hold_id`, so a request_id in facts
                    # would (a) key a per-request singleton "run" whenever
                    # workflow_run_id is absent (the edge always mints a
                    # request_id), and (b) flip run_id_is_fallback to False,
                    # breaking the "synthetic run" audit filter. group_id is pure
                    # attribution (not in the run_id chain), so it is safe.
                    "run_id": context.workflow_run_id,
                    "group_id": context.group_id,
                    # Hard-ceiling overrun record (see above). Named
                    # "admission_checked_microusd" here (NOT the bare
                    # "reserved_microusd" the ledger item ultimately uses,
                    # matching docs/design/hard-ceiling.md section 9's own words
                    # for this field: "the amount admission checked") because
                    # this module is scanned by `tests/billing_guards.py`'s
                    # write-discipline guard, which fail-closes on any bare
                    # string CONTAINING "reserved_microusd"/"settled_microusd"
                    # to catch dynamically-assembled pool-counter
                    # UpdateExpression attribute names — a real, valuable
                    # check for the counters that table mutates. This dict is
                    # plain Python data flowing to a ledger Put on a DIFFERENT
                    # table, never an UpdateExpression, but the guard cannot
                    # tell the two apart from a bare string (a prefixed
                    # "hold_reserved_microusd" still CONTAINS the flagged
                    # fragment and does not clear it), so the rename avoids a
                    # false positive without touching the guard itself.
                    "admission_checked_microusd": _reserved_microusd,
                    "overrun_microusd": _overrun_microusd,
                    "reserve_pricing_version": _reserve_pricing_version,
                    "bound_mode": context.bound_mode,
                    "estimate_inputs": _estimate_inputs,
                },
            )
        except Exception:  # noqa: BLE001
            # The pool settle must never prevent the UsageLogs write below: a
            # non-ClientError (e.g. ReadTimeoutError) here would otherwise lose
            # the audit record of a Bedrock call that already happened.
            logger.error(
                "pool_settle_failed",
                tenant_id=user.org_id,
                period=context.period,
                reserved_microusd=context.pool_reserved_microusd,
                actual_microusd=actual_cost_microusd,
                error_code="non_client_error",
            )
        # Settle the per-model quota too: move `used` from the reserved estimate
        # to the actual spend (actual<=reserved so used only ever decreases here).
        _settle_quota_for(context, int(actual_cost_microusd))
        # P0 decision log: fire-and-forget the OUTCOME record — the measured
        # charge (from the frozen rating we just wrote) plus the counterfactual
        # savings against the requested / max-servable baselines at THIS request's
        # actual tokens. Never blocks or fails settle.
        try:
            from .learning.decision_log import record_outcome_from_context
            record_outcome_from_context(
                context,
                actual_total_cost_microusd=int(actual_cost_microusd),
                actual_input_tokens=int(actual_input_tokens),
                actual_output_tokens=int(actual_output_tokens),
                ledger_pricing_version=(
                    _rating.pricing_version if _rating is not None else None
                ),
            )
        except Exception:  # noqa: BLE001 — decision logging never breaks settle.
            pass

    # ALWAYS record usage, even if the pool settle above failed: the Bedrock
    # call happened and its cost must be auditable. This is deliberately outside
    # any pool try/except so a settle fault cannot swallow the ledger entry.
    # P0-11 visibility: store the client-requested model in the SAME spelling
    # space as the effective `model_id` (its bedrock id), so the read layer can
    # decide fallback with a plain string compare — no read-time canonicalization,
    # hence immune to registry drift/retirement (Fable #65 rev1 BUG 1: an
    # asymmetric canonical-vs-bedrock compare false-positived once a model left
    # the registry). resolve_bedrock_model is total-ish: it raises only for a
    # never-registered id, in which case we fall back to the raw string (a
    # non-empty requested must never fail the ALWAYS-record invariant).
    requested = requested_model or (context.requested_model if context else None)
    requested_stored = None
    if requested:
        try:
            # General registry resolve (handles Claude AND OpenAI families) so
            # the stored requested id matches how the effective `model_id` is
            # spelled (bedrock id). resolve_bedrock_model is Claude-only and
            # would leave OpenAI ids un-normalized -> spurious fallback.
            from .models import resolve_model as _resolve
            requested_stored = _resolve(requested).bedrock_model_id
        except Exception:
            # Residual (Fable #65 rev2): if `requested` is not in the registry
            # (retirement race / out-of-registry chain entry), we store the raw
            # string, which won't equal the bedrock effective id -> that single
            # row reads as a fallback. Window-scoped and non-retroactive
            # (stored bytes are stable); acceptable for P1 visibility.
            requested_stored = requested
    # The UsageLogs SK embeds this id (`log_id = request_id or uuid4()`). Passing
    # the request's own id — not a fresh uuid — is what lets the offline VSR
    # reconciliation (mvp.learning.vsr_reconcile) JOIN a usage row back to its
    # reserve-time decision record (both keyed by the same span_id/request_id).
    # Fall back to the reserve context's id, then to None (a bare uuid) for the
    # rare call site that has neither.
    settle_request_id = request_id or (
        getattr(context, "request_id", None) if context is not None else None
    )
    UsageLogsRepository().record(
        tenant_id=user.org_id,
        user_id=user.user_id,
        user_email=user.email,
        model_id=model_id,
        input_tokens=actual_input_tokens,
        output_tokens=actual_output_tokens,
        cost_microusd=actual_cost_microusd,
        requested_model_id=requested_stored,
        request_id=settle_request_id,
        # Coordinator ITEM 2: the `measured` bound's destination is this
        # per-request, append-only usage row — never the ledger (which would
        # need a pool row, real or synthesised, to attach to). None whenever
        # the bound was never computed for this request (the `accounting`
        # state; see `mvp.reservation_bound.dollar_pool_bound_should_compute`).
        measured_bound_microusd=(
            context.measured_bound_microusd if context is not None else None
        ),
    )


def _settle_pool_side(
    user,
    context,
    actual_cost_microusd: int,
    *,
    ledger_facts: Optional[dict] = None,
) -> None:
    """Move this reservation's `reserved` into `settled` and delete its hold in
    one transaction, with the double-subtract and vanished-row races handled.

    The transaction is [aggregate settle, conditional hold delete, ledger SETTLE
    event]. Cancel outcomes are reconciled by inspecting per-item
    CancellationReasons:
      - hold-delete (index 1) failed ConditionalCheckFailed → the reaper already
        reclaimed this hold AND already returned its reserved share, so we must
        NOT subtract reserved again; record settled-only instead;
      - aggregate (index 0) failed ConditionalCheckFailed → the pool row was
        deleted mid-flight (pool_vanished) → nothing to reconcile, no-op;
      - ledger (index 2) failed ConditionalCheckFailed → a terminal event for
        this hold already exists (retried settle, or a reaper reclaim beat us) →
        already finalized, treat as idempotent success;
      - anything else → transient, retry with the same idempotency token.

    The ledger SETTLE event (P0-1) is written in the SAME transaction as the
    counter move, so spend is recorded iff `pool_settled` advances. Its sk
    (`EV#HOLD#<hold_id>#TERMINAL`) with `attribute_not_exists` is the app-level
    idempotency guard — the ClientRequestToken only dedupes botocore's transparent
    retries, not an application re-invocation. Ledger emission is best-effort in
    the sense that a missing `hold_id` (should not happen for a pooled reserve)
    skips it rather than blocking the settle.
    """
    budgets = TenantBudgetsRepository()
    # TransactItems ORDER IS A CONTRACT: index 0 = pool-row settle, index 1 =
    # hold delete, index 2 = ledger SETTLE. The cancellation-reason parsing below
    # reads reasons[_POOL_IDX] / reasons[_HOLD_IDX] / reasons[_LEDGER_IDX] by
    # position, so these must stay in sync with the order items are appended.
    # Indices are assigned as items are appended (the hold-delete item is
    # conditional), NOT statically — a static _HOLD_IDX=1 would alias the ledger
    # item when the hold item is absent (Fable impl review Bug 3). None means
    # "this item is not in the transaction".
    _POOL_IDX = 0
    _HOLD_IDX: Optional[int] = None
    _LEDGER_IDX: Optional[int] = None
    # Resolve hold_id up front so the pool settle item can REMOVE this hold's
    # PENDING-protocol `applied` marker in the SAME write (no-op for a
    # transaction-mode hold that has no marker).
    _hold_id_for_marker = context.hold_id
    if not _hold_id_for_marker and context.hold_sk:
        _hold_id_for_marker = context.hold_sk.rsplit("#", 1)[-1] or None
    items = [
        _pool_settle_items(
            table_name=budgets.table_name,
            tenant_id=user.org_id,
            period=context.period,
            reserved_microusd=context.pool_reserved_microusd,
            actual_microusd=actual_cost_microusd,
            hold_id=_hold_id_for_marker,
        )
    ]
    if context.hold_sk:
        _HOLD_IDX = len(items)
        items.append(
            budgets.hold_delete_txn_item(tenant_id=user.org_id, sk=context.hold_sk)
        )

    # Resolve the hold_id: prefer the explicit one, else parse it from the hold
    # sk (`HOLD#<period>#<expires>#<hold_id>`). The ledger must not be silently
    # skipped when the pool counter moves, or I1 (Σsettled == pool_settled)
    # breaks for a legitimate settle (Fable impl review Bug 4).
    _hold_id = context.hold_id
    if not _hold_id and context.hold_sk:
        _hold_id = context.hold_sk.rsplit("#", 1)[-1] or None

    _ledger_item = None
    # A separate ledger item for the reaper-race (settled-only) path: there the
    # reaper already returned `reserved`, so the counter moves settled-ONLY —
    # the ledger event must mirror that with reserved_delta=0, not -reserved
    # (Fable impl review Bug 1). Both are terminal SETTLEs on the same sk, so
    # attribute_not_exists still makes them mutually exclusive / idempotent.
    _ledger_item_settled_only = None
    if _hold_id:
        from dynamo import CreditLedgerRepository

        facts = ledger_facts or {}
        _real_run = facts.get("run_id") or facts.get("request_id")
        _run_id = _real_run or _hold_id
        _run_is_fallback = _real_run is None
        _ledger = CreditLedgerRepository()

        def _mk_settle_event(reserved_delta: int, reason: str):
            return _ledger.terminal_event_txn_item(
                tenant_id=user.org_id,
                period=context.period,
                hold_id=_hold_id,
                event_type="SETTLE",
                reserved_delta_microusd=reserved_delta,
                settled_delta_microusd=int(actual_cost_microusd),
                run_id=_run_id,
                run_id_is_fallback=_run_is_fallback,
                span_id=facts.get("span_id"),
                request_id=facts.get("request_id"),
                group_id=facts.get("group_id"),
                model_id=context.selected_model or facts.get("model_id"),
                pricing_version=facts.get("pricing_version"),
                pricing_key=facts.get("pricing_key"),
                rating=facts.get("rating"),
                tokens_in=facts.get("tokens_in"),
                tokens_out=facts.get("tokens_out"),
                settle_reason=reason,
                # Hard-ceiling overrun record (docs/design/hard-ceiling.md item 4):
                # passed straight through from what settle_reservation_and_log
                # computed — this builder has no opinion on the money, only on
                # how to shape the Put.
                reserved_microusd=facts.get("admission_checked_microusd"),
                overrun_microusd=facts.get("overrun_microusd"),
                reserve_pricing_version=facts.get("reserve_pricing_version"),
                bound_mode=facts.get("bound_mode"),
                estimate_inputs=facts.get("estimate_inputs"),
                # Absence would have to mean "inline" if it were left unset, and a
                # value that has to be inferred from absence is the defect shape
                # this contract is organised around. The inline settle passes no
                # source, so it is named here rather than implied.
                source=facts.get("source") or "inline",
            )

        _ledger_item = _mk_settle_event(
            -int(context.pool_reserved_microusd),
            facts.get("settle_reason") or "completion",
        )
        _ledger_item_settled_only = _mk_settle_event(0, "reaper_race")
        _LEDGER_IDX = len(items)
        items.append(_ledger_item)
    # One fresh token, generated ONCE and reused across our explicit retries: the
    # settle params are timestamp-free, so a retry after a lost ack carries the
    # same token+params and DynamoDB dedupes it to success instead of double-
    # applying. A fresh UUID keeps it distinct from any other request's settle.
    token = _fresh_idempotency_token()
    client = _low_level_client()
    for _attempt in range(_SETTLE_MAX_RETRIES):
        if _attempt:
            # Tighter cap than reserve: settle runs on the event loop at the
            # tail of the streaming path, so a long sleep here freezes every
            # co-located stream.
            time.sleep(_contention_backoff(_attempt, cap=_SETTLE_BACKOFF_CAP_SECONDS))
        try:
            client.transact_write_items(TransactItems=items, ClientRequestToken=token)
            # PENDING protocol (docs/design/pending-protocol.md, PR-1): the pool
            # settle above already returned this hold's headroom atomically and
            # deleted the hold, so the separate marker item plays no money role
            # here — settle it (RESERVED -> SETTLED + TTL) as cleanup so it stops
            # looking outstanding and becomes GC-eligible. Best-effort: money-safety
            # does not depend on it (a settled hold is never EXPIRED_UNCREDITED, so
            # the reconciler can never credit it), and the reconcile audit sweep
            # settles any marker this misses.
            if _reserve_protocol_for(user.org_id) == "pending" and _hold_id_for_marker:
                budgets.marker_settle_best_effort(
                    tenant_id=user.org_id, hold_id=_hold_id_for_marker)
            return  # settled cleanly (reserved returned, spend recorded, hold gone)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "TransactionCanceledException":
                reasons = _cancellation_codes(e)
                # Reading reasons by position is sound ONLY because each item's
                # ConditionExpression is single-clause: the pool item (index
                # _POOL_IDX) is guarded solely by `attribute_exists(tenant_id)`
                # (see _pool_settle_items) and the hold delete (index _HOLD_IDX)
                # solely by `attribute_exists(sk)`. So a ConditionalCheckFailed
                # at that index unambiguously means "row/hold gone", never a
                # contention/underflow guard. If either condition ever becomes
                # compound, disambiguate via ReturnValuesOnConditionCheckFailure
                # instead of trusting the reason index.
                # Ledger idempotency: a ConditionalCheckFailed on the ledger item
                # means a TERMINAL event for this hold already exists — this
                # reservation was already finalized (a retried settle, or the
                # reaper reclaimed+recorded first). The whole transaction was
                # cancelled, so the counters were NOT double-moved; treat it as an
                # idempotent success and stop. Checked FIRST because it subsumes
                # the settle having already happened.
                ledger_dup = (
                    _LEDGER_IDX is not None
                    and len(reasons) > _LEDGER_IDX
                    and reasons[_LEDGER_IDX] == "ConditionalCheckFailed"
                )
                if ledger_dup:
                    # A TERMINAL event for this hold already exists. WHY decides
                    # what we do (Phase 2 — no more blind return):
                    #   SETTLE  → this settle already happened → idempotent success
                    #   RELEASE → client abandoned the hold → already_released, and
                    #             we must NOT record spend (protocol: a released
                    #             hold is not billable)
                    #   RECLAIM → the reaper reclaimed the hold (returned reserved)
                    #             before we settled → record the spend via a
                    #             LATE_SETTLE, or it is lost (the revenue leak this
                    #             phase closes)
                    existing = _ledger.get_terminal(
                        tenant_id=user.org_id,
                        period=context.period,
                        hold_id=_hold_id,
                    )
                    _ev_type = (existing or {}).get("event_type")
                    if _ev_type == "RECLAIM":
                        # External authorize/capture (Fable authcap D-2): an
                        # external hold that the reaper already reclaimed must NOT
                        # be recovered via LATE_SETTLE. Unlike an inline request
                        # (whose reserve→settle window is seconds, so the reclaimed
                        # reserved has almost certainly not been re-lent), an
                        # external capture window is tenant-controlled and
                        # unbounded — the returned reserved may already back a
                        # different authorize, so late-billing here could push
                        # spent past limit. Signal the capture endpoint to return
                        # 410 (expired) instead; the counters are untouched (the
                        # whole txn cancelled), so no spend and no leak.
                        if (getattr(context, "source", None) or "") == "external":
                            logger.info(
                                "external_capture_hold_reclaimed_410",
                                tenant_id=user.org_id,
                                period=context.period,
                                hold_id=_hold_id,
                            )
                            raise ExternalHoldReclaimed(_hold_id)
                        logger.info(
                            "pool_settle_hold_reclaimed_recovering_spend",
                            tenant_id=user.org_id,
                            period=context.period,
                            hold_id=_hold_id,
                            actual_microusd=actual_cost_microusd,
                        )
                        _recover_spend_via_late_settle(
                            client=client,
                            ledger=_ledger,
                            budgets_table_name=budgets.table_name,
                            tenant_id=user.org_id,
                            period=context.period,
                            hold_id=_hold_id,
                            actual_microusd=actual_cost_microusd,
                            run_id=_run_id,
                            run_is_fallback=_run_is_fallback,
                            facts=facts,
                        )
                        return
                    if _ev_type == "RELEASE":
                        # Late settle after an explicit release: protocol violation
                        # (the client abandoned this reservation). Do NOT bill it.
                        logger.warning(
                            "pool_settle_after_release_ignored",
                            tenant_id=user.org_id,
                            period=context.period,
                            hold_id=_hold_id,
                        )
                        return
                    if _ev_type == "SETTLE":
                        # The settle already landed → idempotent success. The
                        # counters were NOT double-moved (the whole txn cancelled).
                        logger.info(
                            "pool_settle_already_finalized_in_ledger",
                            tenant_id=user.org_id,
                            period=context.period,
                            hold_id=_hold_id,
                        )
                        return
                    # None / unknown terminal type. get_terminal is ConsistentRead,
                    # so a CCF at _LEDGER_IDX (terminal already exists) can NOT read
                    # back None or an unrecognised type unless there is a real defect
                    # — an index/position mismatch in the txn, or a pk/period
                    # mismatch between the write and the read. Returning "idempotent
                    # success" here would silently DROP the spend. Treat it as an
                    # invariant violation: error + raise. NOTE (Fable P2 review-2
                    # R2-4): settle has no client retry (streaming tail), so this
                    # raise is absorbed by the outer best-effort settle into a
                    # `pool_settle_failed` log — it is an ALARM signal
                    # (`pool_settle_terminal_unclassified`, alarmed in iac), not a
                    # self-healing redrive. That is still strictly better than a
                    # silent success; the defect it flags should never occur.
                    logger.error(
                        "pool_settle_terminal_unclassified",
                        tenant_id=user.org_id,
                        period=context.period,
                        hold_id=_hold_id,
                        terminal_type=_ev_type,
                    )
                    raise
                hold_gone = (
                    _HOLD_IDX is not None
                    and len(reasons) > _HOLD_IDX
                    and reasons[_HOLD_IDX] == "ConditionalCheckFailed"
                )
                row_gone = (
                    len(reasons) > _POOL_IDX and reasons[_POOL_IDX] == "ConditionalCheckFailed"
                )
                if hold_gone:
                    # The hold row is gone but the ledger TERMINAL clash did NOT
                    # fire — so no terminal exists for this hold. In Phase 2 the
                    # reaper writes its RECLAIM terminal in the SAME txn as the hold
                    # delete, so `hold gone AND no terminal` can only be a LEGACY
                    # pre-Phase-2 hold (reclaimed by an old reaper that wrote no
                    # ledger event). Fall back to the Phase-1 behaviour: record the
                    # spend settled-only (reaper already returned reserved) with a
                    # settled-only SETTLE terminal, and emit a metric so operators
                    # can confirm the legacy tail has drained before this fallback
                    # is removed (see P2-d / rollout step 7).
                    logger.error(
                        "LegacyHoldNoTerminal",
                        tenant_id=user.org_id,
                        period=context.period,
                        hold_id=_hold_id,
                        reserved_microusd=context.pool_reserved_microusd,
                        actual_microusd=actual_cost_microusd,
                    )
                    _so_items = [
                        _settled_only_txn_item(
                            table_name=budgets.table_name,
                            tenant_id=user.org_id,
                            period=context.period,
                            actual_microusd=actual_cost_microusd,
                        )
                    ]
                    if _ledger_item_settled_only is not None:
                        _so_items.append(_ledger_item_settled_only)
                    try:
                        client.transact_write_items(
                            TransactItems=_so_items,
                            # Derive from the primary settle token (not a fresh
                            # UUID) so a lost-ack here that gets retried dedupes
                            # to the same write instead of double-recording spend.
                            # Must stay <=36 chars: f"{token}-so" would be 39 and
                            # ValidationException every time (silent revenue leak
                            # on the reaper-race path). uuid5 keeps it exactly 36.
                            ClientRequestToken=_derived_token(token, "settled-only"),
                        )
                    except ClientError as e2:
                        if (
                            e2.response.get("Error", {}).get("Code")
                            != "TransactionCanceledException"
                        ):
                            raise
                        # Cancelled → pool row also gone, or a terminal ledger event
                        # already exists (already finalized) → nothing to do.
                    return
                if row_gone:
                    # Pool row deleted mid-flight (pool_vanished): no reservation
                    # to reconcile, and we must not resurrect a ghost row.
                    logger.info(
                        "pool_settle_row_vanished",
                        tenant_id=user.org_id,
                        period=context.period,
                    )
                    return
            # duplicate token or transient capacity → log and retry with the
            # same token (a genuine duplicate dedupes; a transient one succeeds).
            logger.warning(
                "pool_settle_retry",
                tenant_id=user.org_id,
                period=context.period,
                attempt=_attempt,
                error_code=code,
            )
            continue
    # Retries exhausted. The hold still ties up pool budget until the reaper
    # reclaims it at TTL, and that reclaim records a settled delta of ZERO — so
    # before this row existed, usage the provider had already reported left the
    # system here, with a log line as its only trace (C3.5). The row is what the
    # reaper reads to recover it through the LATE_SETTLE path it already uses when
    # it wins the race against a settle: the charge is re-driven by durable state
    # rather than by a human noticing an alarm.
    #
    # Written only on this path, so the happy path pays nothing for it. What it does
    # NOT cover is stated rather than implied: a task that dies between learning the
    # usage and writing this row still loses it, because covering that needs a
    # write-ahead on every settle — a cost on every request, not a rare one.
    if _hold_id:
        _ledger.put_owed_settle(
            tenant_id=user.org_id,
            period=context.period,
            hold_id=_hold_id,
            actual_microusd=int(actual_cost_microusd),
            run_id=_run_id,
            run_id_is_fallback=_run_is_fallback,
            facts=(ledger_facts or {}),
        )
    logger.error(
        "pool_settle_failed",
        tenant_id=user.org_id,
        period=context.period,
        reserved_microusd=context.pool_reserved_microusd,
        actual_microusd=actual_cost_microusd,
        owed_recorded=bool(_hold_id),
    )
    # The reaper reads for an owed row AFTER it commits its RECLAIM, so one
    # interleaving is left over: it read and found nothing, and this row was written
    # a moment later. The hold is gone by then, so no later sweep revisits it and the
    # charge would sit in a row nobody consults. Closing it needs no new mechanism,
    # only the other order: this row was written before the read below, so if a
    # RECLAIM is already visible here, the reclaim has happened and it is this side's
    # turn to recover. Together the two orders are total — whichever party is second
    # sees the other's write.
    if _hold_id:
        _redrive_owed_after_late_reclaim(
            budgets=budgets, ledger=_ledger, tenant_id=user.org_id,
            period=context.period, hold_id=_hold_id)
