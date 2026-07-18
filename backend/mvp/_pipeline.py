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
from typing import TYPE_CHECKING, Optional

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
_RESERVE_MAX_RETRIES = 12
_RESERVE_BACKOFF_SECONDS = 0.01  # base delay for the exponential backoff below.
_RESERVE_BACKOFF_CAP_SECONDS = 0.4  # ceiling so a hot row can't stall a request.
# Settlement must not fail a live request; it retries a few times against
# transient capacity errors before giving up loudly (a lost settle leaks the
# hold, so it is logged at error level for reconciliation).
_SETTLE_MAX_RETRIES = 4
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
# Reclaim only a handful of expired holds per request so the sweep never turns
# the hot reserve path into an unbounded scan.
_SWEEP_MAX_HOLDS = int(os.getenv("STRATOCLAVE_POOL_SWEEP_MAX_HOLDS", "5"))


def _pool_settle_items(
    *,
    table_name: str,
    tenant_id: str,
    period: str,
    reserved_microusd: int,
    actual_microusd: int,
    reclaimed_microusd: int = 0,
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

    expr = "ADD pool_reserved_microusd :dr, pool_settled_microusd :actual"
    values = {
        ":dr": {"N": str(-int(reserved_microusd))},
        ":actual": {"N": str(int(actual_microusd))},
    }
    if reclaimed_microusd:
        expr += ", pool_reclaimed_microusd :rec"
        values[":rec"] = {"N": str(int(reclaimed_microusd))}
    return {
        "Update": {
            "TableName": table_name,
            "Key": {
                "tenant_id": {"S": tenant_id},
                "sk": {"S": budget_sk(period)},
            },
            "UpdateExpression": expr,
            "ConditionExpression": "attribute_exists(tenant_id)",
            "ExpressionAttributeValues": values,
        }
    }


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
    # True when a rate snapshot was attempted at reserve but the rate-table read
    # failed — settle then charges via the live-rate fallback and labels the
    # terminal with the snapshot-failed sentinel (distinct from legacy).
    rate_snapshot_failed: bool = False
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
        items = [
            _pool_settle_items(
                table_name=budgets.table_name,
                tenant_id=self.tenant_id,
                period=self.period,
                reserved_microusd=self.pool_reserved_microusd,
                actual_microusd=0,
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
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "TransactionCanceledException":
                # Cancelled because the hold was already reclaimed by the reaper
                # (reserved already returned) or the pool row is gone — either
                # way there is nothing left to release. Expected, not an error.
                logger.info(
                    "pool_release_noop_already_reconciled",
                    tenant_id=self.tenant_id,
                    period=self.period,
                    reserved_microusd=self.pool_reserved_microusd,
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


def _sweep_one_period(budgets, tenant_id: str, period: str, cap: int) -> int:
    """Reclaim up to `cap` expired holds for one tenant/period. See
    `_sweep_expired_holds` for the contract; this is the per-period worker."""
    if cap <= 0:
        return 0
    reclaimed = 0
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
        try:
            _reaper_items = [
                _pool_settle_items(
                    table_name=budgets.table_name,
                    tenant_id=tenant_id,
                    period=period,
                    reserved_microusd=amount,
                    actual_microusd=0,
                    reclaimed_microusd=amount,
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
                    )
                )
            client.transact_write_items(
                TransactItems=_reaper_items,
                ClientRequestToken=_fresh_idempotency_token(),
            )
            reclaimed += 1
            # error level, with amount: an orphan means a request that reserved
            # budget then vanished. If the crash was AFTER a successful Bedrock
            # call, real spend happened but is recorded here as actual=0 — this
            # line + pool_reclaimed_microusd let operators reconcile the bill.
            logger.error(
                "pool_hold_reclaimed",
                tenant_id=tenant_id,
                period=period,
                hold_id=hold_id,
                amount_microusd=amount,
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
    saar_warm_prefix_tokens: int = 0,
    saar_prefer_model: Optional[str] = None,
    vsr_decision: Optional[dict] = None,
) -> ReservationContext:
    """Reserve credit for a request, with per-model quota + cascading fallback.

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
    from .routing.config import get_tenant_routing_config, get_user_routing_config

    def _price(model: str) -> tuple[str, int]:
        try:
            pk = _resolve_pricing(model).pricing_key
        except ValueError:
            pk = "default"
        # SAAR: when this request is served by the session's warm model,
        # `saar_warm_prefix_tokens` of the input estimate re-bill at the cheaper
        # cache-read rate — so staying warm reserves LESS than switching cold, and
        # a switch that would breach the pool while a stay would fit is naturally
        # gated at the 402. In P0 this is always 0 (cache evidence lands in P1), so
        # the estimate is byte-identical to pre-SAAR. The discount applies ONLY to
        # the warm model itself — the tool-loop hard pin (`vsr_hard_model`) or the
        # sticky soft preference (`saar_prefer_model`) — never to a cold candidate.
        warm_model = vsr_hard_model or saar_prefer_model
        warm = saar_warm_prefix_tokens if (warm_model and model == warm_model) else 0
        return pk, estimate_cost_microusd(
            pricing_key=pk,
            input_tokens_est=input_tokens_est,
            max_output_tokens=max_output_tokens,
            effort_multiplier=effort_multiplier,
            warm_prefix_tokens=warm,
        )

    tenant_cfg = get_tenant_routing_config(user.org_id)

    # VSR hard pin (P0-15): validate, then force the candidate list to exactly
    # [pin] and fall through to the same reserve loop (pricing + quota + atomic
    # reserve) — so pinning reuses all the money machinery, only the candidate
    # SELECTION changes. Handled before the no-config passthrough so a pin is
    # honoured whether or not the tenant has routing config.
    def _stamp_requested(ctx: ReservationContext) -> ReservationContext:
        # Record the client-requested model (pre-cascade) on the context so
        # settle can log P0-11 fallback visibility. Single chokepoint => every
        # handler gets it without threading through each reserve return path.
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
        )
        # A VSR hard pin is a deliberate policy override, NOT a P0-11 quota
        # cascade fallback (Fable #65 rev1 BUG 2). Record the effective (pinned)
        # model as the "requested" one so the pin never inflates fallback_count
        # or shows a spurious fallback badge — the two events are semantically
        # different and the derived bool must not conflate them.
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
        pk, cost = _price(model_name)
        return _stamp_requested(reserve_credit(
            user, reservation_tokens,
            pricing_key=pk, cost_microusd=cost,
            selected_model=model_name,
        ))

    user_cfg = get_user_routing_config(user.org_id, user.user_id)
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
    ))


def _reserve_over_candidates(user, reservation_tokens, *, candidates, tenant_cfg, price):
    """Walk an ordered candidate list, pricing + quota-reserving each atomically.

    Shared by the P0-11 cascade and the P0-15 hard pin (a pin is just a
    one-element candidate list). QuotaExhausted advances to the next candidate;
    if every candidate's quota is exhausted, 402 `model_quota_exhausted` (for a
    single-element pin list that means: the pinned model's quota is gone, no
    fallback — the hard-pin contract)."""
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
        pk, cost = price(model)
        priced_tried.append((model, pk, cost))
        q = tenant_cfg.quotas.get(model)
        tenant_limit = q.limit if q else None
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
    priced = list(priced_tried) + [(m, *price(m)) for m in untried_models]
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
    otherwise-valid direct request. If filtering empties the chain, we keep the
    requested model as the sole candidate.
    """
    from .models import resolve_model as _resolve_registry
    from .routing.model_resolver import _resolve_chain, resolve_model

    fallback_allowed = (tenant_cfg.fallback_default == "on")
    if user_cfg and user_cfg.fallback is not None:
        fallback_allowed = (user_cfg.fallback == "on")

    selection = resolve_model(
        requested_model=requested_model,
        tenant_config=tenant_cfg,
        user_config=user_cfg,
        breaker_max_tier=breaker_max_tier,
        fallback_allowed=fallback_allowed,
    )

    candidates = _resolve_chain(requested_model, tenant_cfg, user_cfg)
    if tenant_cfg.allowlist:
        candidates = [m for m in candidates if m in tenant_cfg.allowlist] or [selection.selected_model]
    if breaker_max_tier is not None:
        from .routing.chains import _tier_for
        capped = [m for m in candidates if _tier_for(m) <= breaker_max_tier]
        candidates = capped or candidates
    if not fallback_allowed:
        candidates = candidates[:1]
    candidates = candidates or [selection.selected_model]

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
    return servable or [requested_model]


def reserve_credit(
    user,
    reservation_tokens: int,
    *,
    pricing_key: Optional[str] = None,
    cost_microusd: Optional[int] = None,
    quota_lines: Optional[list] = None,
    quota_model: Optional[str] = None,
    selected_model: Optional[str] = None,
) -> ReservationContext:
    """Atomically reserve budget before invoking Bedrock.

    - Without a tenant pool budget (or when `cost_microusd` is not supplied):
      debits `reservation_tokens` from the per-user balance exactly as before.
    - With a tenant pool budget for the current period: debits the per-user
      tokens AND reserves `cost_microusd` from the pool in one transaction.

    Returns a `ReservationContext` for the settle step. Raises HTTP 402 with a
    `reason` of `personal_budget_exhausted` or `tenant_pool_exhausted`.
    """
    repo = UserTenantsRepository()
    repo.ensure(user_id=user.user_id, tenant_id=user.org_id)

    period = current_period()
    # Layer 5: freeze the rate NOW (reserve time) so settle rates the charge at
    # the admitted version even if the live table is flipped later. Only when a
    # pricing_key is known (priced reservation); a rate-table blip must never fail
    # the reserve, so a snapshot failure degrades to None (settle then falls back
    # to the legacy live-rate path). Shared across every context return below.
    from .pricing import (
        SNAPSHOT_FAILED_SENTINEL as _SNAP_FAILED,
        UNVERSIONED_SENTINEL as _UNVERSIONED,
    )

    _rate_snap = None
    _snap_failed = False
    if pricing_key:
        try:
            from .pricing import snapshot_rates
            _rate_snap = snapshot_rates(pricing_key)
        except Exception:  # noqa: BLE001 — pricing must never break admission
            # DEGRADED PATH (Fable review-2 N3): the rate table read failed, so
            # settle will charge via the live-rate fallback and the terminal is
            # labeled `snapshot-failed` (a DISTINCT sentinel from legacy). error
            # level (not warning) so a CloudWatch metric filter can alarm — a
            # persistent rate-table outage silently degrading all charging is the
            # exact failure mode Layer 5 must not hide.
            _snap_failed = True
            logger.error("RateSnapshotFailed", pricing_key=pricing_key)
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
            rate_snapshot_failed=_snap_failed,
            tenant_id=user.org_id,
            pool_active=False,
            selected_model=selected_model,
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
            )
            raise _err_402("tenant_pool_exhausted")

        p_limit = int(pool_row.get("pool_limit_microusd", 0))
        p_reserved = int(pool_row.get("pool_reserved_microusd", 0))
        p_settled = int(pool_row.get("pool_settled_microusd", 0))
        if p_reserved + p_settled + cost > p_limit:
            logger.info(
                "credit_exhausted_402",
                user_id=user.user_id,
                tenant_id=user.org_id,
                reason="tenant_pool_exhausted",
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
            expected_reserved=p_reserved,
            expected_settled=p_settled,
        )
        hold_txn = budgets.hold_put_txn_item(
            tenant_id=user.org_id,
            period=period,
            hold_id=hold_id,
            amount_microusd=cost,
            expires_at_epoch=hold_expires_at,
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
                    pricing_version=(
                        _rate_snap.version if _rate_snap is not None
                        else (_SNAP_FAILED if _snap_failed else _UNVERSIONED)
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
            rate_snapshot_failed=_snap_failed,
            tenant_id=user.org_id,
            pool_active=True,
            hold_id=hold_id,
            hold_sk=hold_sk,
            quota_lines=quota_lines,
            selected_model=selected_model,
            quota_reserved_amount=cost if quota_lines else 0,
            quota_user_id=user.user_id,
            quota_period=period if quota_lines else None,
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
            rate_snapshot_failed=_snap_failed,
            tenant_id=user.org_id,
            pool_active=False,
            selected_model=selected_model,
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
        return _idemp_replay(prior, request_fingerprint)

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
            expected_reserved=p_reserved,
            expected_settled=p_settled,
        )
        hold_txn = budgets.hold_put_txn_item(
            tenant_id=tenant_id,
            period=period,
            hold_id=hold_id,
            amount_microusd=amount,
            expires_at_epoch=hold_expires_at,
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
        try:
            client.transact_write_items(
                TransactItems=[pool_txn, hold_txn, reserve_evt, idemp_txn],
                ClientRequestToken=_fresh_idempotency_token(),
            )
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
                    return _idemp_replay(winner, request_fingerprint)
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
    """Read the IDEMP row for a key in `period`, falling back to the previous
    period (Fable authcap review-1 H-2: a retry crossing a month boundary must
    still find the original). Both reads are ConsistentRead. Returns the row or
    None. ttl_max is 24h so the original can only be one period back."""
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


def _idemp_replay(idemp_row: dict, request_fingerprint: str) -> ExternalAuthorizeResult:
    """Reconstruct an ExternalAuthorizeResult from a stored IDEMP row (a
    duplicate-key replay). The row froze everything the authorize response needs,
    so a replay is a pure read — no rehydrate, no second reserve.

    First it verifies the incoming request's fingerprint matches the stored one:
    a mismatch means the key was reused for a different request (or a sanitize
    collision), which must be a 422, never a wrong-authorization replay (H-1).
    A MISSING stored fingerprint is also a mismatch (Fable authcap review-4 M-C):
    every IDEMP row this code writes carries one, so an absent fingerprint means
    a partial write / hand-inserted / foreign row — replaying it could hand back
    an authorization for a different body, so reject rather than skip the check."""
    stored_fp = str(idemp_row.get("request_fingerprint", ""))
    if stored_fp != request_fingerprint:
        raise IdempotencyKeyReuse(
            "Idempotency-Key reused for a different request"
        )
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
    ledger = _reaper_ledger()
    reserve_evt = ledger.get_reserve(
        tenant_id=tenant_id, period=period, hold_id=hold_id
    )
    # C-1 gate: only holds minted by the external authorize API are rehydratable.
    if reserve_evt is None or reserve_evt.get("source") != "external":
        return None

    budgets = TenantBudgetsRepository()
    hold = budgets.get_hold(tenant_id=tenant_id, sk=hold_sk)
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
) -> ReservationContext:
    """Reserve per-user tokens AND a per-model quota atomically, with NO pool.

    For tenants that configure a per-model quota but no dollar pool. Same
    snapshot-optimistic retry as the pooled path, but the transaction is just
    [user_txn, *quota_lines] — no pool debit, no HOLD row. A quota
    ConditionalCheckFailed (index >= 1) means the quota is exhausted → raise
    QuotaExhausted so the caller's cascade advances; a user-row CCF (index 0) is
    the retryable snapshot race. Fails closed: a quota-configured request must
    never slip through unmetered (the Fable F-3 hole).
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
    """
    from dynamo.tenant_budgets import budget_sk

    return {
        "Update": {
            "TableName": table_name,
            "Key": {"tenant_id": {"S": tenant_id}, "sk": {"S": budget_sk(period)}},
            "UpdateExpression": "ADD pool_settled_microusd :actual",
            "ConditionExpression": "attribute_exists(tenant_id)",
            "ExpressionAttributeValues": {":actual": {"N": str(int(actual_microusd))}},
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
            logger.info(
                "pool_settle_late_settle_recovered",
                tenant_id=tenant_id,
                period=period,
                hold_id=hold_id,
                actual_microusd=actual_microusd,
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
    actual_cache_read_tokens: int = 0,
    actual_cache_write_tokens: int = 0,
    requested_model: Optional[str] = None,
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
    from .pricing import SNAPSHOT_FAILED_SENTINEL, UNVERSIONED_SENTINEL

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
                cache_read_tokens=max(actual_cache_read_tokens, 0),
                cache_write_tokens=max(actual_cache_write_tokens, 0),
            )
            actual_cost_microusd = _rating.total_cost_microusd
        else:
            # Legacy / snapshot-less reservation: fall back to the live-rate path
            # (pre-Layer-5 behaviour). No frozen rating record is produced.
            from .pricing import actual_cost_microusd as _price_actual

            actual_cost_microusd = _price_actual(
                pricing_key=context.pricing_key,
                input_tokens=actual_input_tokens,
                output_tokens=actual_output_tokens,
                cache_read_tokens=max(actual_cache_read_tokens, 0),
                cache_write_tokens=max(actual_cache_write_tokens, 0),
            )

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
                    # cost, or a snapshot-less legacy reservation — we must NOT
                    # stamp a version the amount was not derived from (that would
                    # be a false dispute label AND relapse bug#1 by writing the
                    # pricing_key). Use a DISTINCT sentinel per cause instead
                    # (Fable review-2 N2/N3): snapshot-failed vs unversioned-legacy.
                    "pricing_version": (
                        _rating.pricing_version
                        if _rating is not None
                        else (
                            SNAPSHOT_FAILED_SENTINEL
                            if context.rate_snapshot_failed
                            else UNVERSIONED_SENTINEL
                        )
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
    UsageLogsRepository().record(
        tenant_id=user.org_id,
        user_id=user.user_id,
        user_email=user.email,
        model_id=model_id,
        input_tokens=actual_input_tokens,
        output_tokens=actual_output_tokens,
        cost_microusd=actual_cost_microusd,
        requested_model_id=requested_stored,
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
    items = [
        _pool_settle_items(
            table_name=budgets.table_name,
            tenant_id=user.org_id,
            period=context.period,
            reserved_microusd=context.pool_reserved_microusd,
            actual_microusd=actual_cost_microusd,
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
    # Retries exhausted: an unsettled hold ties up pool budget until the reaper
    # reclaims it at TTL. Emit a loud, structured record for reconciliation.
    logger.error(
        "pool_settle_failed",
        tenant_id=user.org_id,
        period=context.period,
        reserved_microusd=context.pool_reserved_microusd,
        actual_microusd=actual_cost_microusd,
    )
