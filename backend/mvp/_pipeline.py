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
from typing import Optional

import boto3
from botocore.exceptions import ClientError
from fastapi import HTTPException

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
    tenant_id: str = ""
    pool_active: bool = False
    quota_lines: list = None  # list[dict] of per-model quota txn items (None = no quota)
    # Per-model quota bookkeeping (set when a quota reservation was committed),
    # so settle/release can move the same model's `used` counter. `selected_model`
    # is the model the cascade actually landed on (may differ from requested).
    selected_model: Optional[str] = None
    quota_reserved_amount: int = 0
    quota_user_id: Optional[str] = None
    # The period the quota `used` counter was reserved against. settle/release
    # MUST key off this, never a fresh current_period() — a long request (or a
    # stream) that crosses a month boundary would otherwise settle the wrong
    # period's row (leaking the reserved period, negative-seeding the new one).
    quota_period: Optional[str] = None
    quota_tenant_limit: Optional[int] = None
    quota_user_limit: Optional[int] = None
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
        try:
            client.transact_write_items(
                TransactItems=[
                    _pool_settle_items(
                        table_name=budgets.table_name,
                        tenant_id=tenant_id,
                        period=period,
                        reserved_microusd=amount,
                        actual_microusd=0,
                        reclaimed_microusd=amount,
                    ),
                    budgets.reclaim_hold_txn_item(tenant_id=tenant_id, sk=sk),
                ],
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
                hold_id=str(hold.get("hold_id", "")),
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
    """
    from .models import resolve_model as _resolve_pricing
    from .pricing import estimate_cost_microusd
    from .routing.config import get_tenant_routing_config, get_user_routing_config

    def _price(model: str) -> tuple[str, int]:
        try:
            pk = _resolve_pricing(model).pricing_key
        except ValueError:
            pk = "default"
        return pk, estimate_cost_microusd(
            pricing_key=pk,
            input_tokens_est=input_tokens_est,
            max_output_tokens=max_output_tokens,
            effort_multiplier=effort_multiplier,
        )

    tenant_cfg = get_tenant_routing_config(user.org_id)
    # No routing config at all → passthrough on the requested model (fully
    # backward compatible: same reservation as before, no quota lines).
    if not tenant_cfg.chain and not tenant_cfg.allowlist and not tenant_cfg.quotas:
        pk, cost = _price(model_name)
        return reserve_credit(
            user, reservation_tokens,
            pricing_key=pk, cost_microusd=cost,
            selected_model=model_name,
        )

    user_cfg = get_user_routing_config(user.org_id, user.user_id)
    candidates = _resolve_candidate_chain(
        requested_model=model_name,
        tenant_cfg=tenant_cfg,
        user_cfg=user_cfg,
        breaker_max_tier=breaker_max_tier,
        wire_protocol=wire_protocol,
    )

    from .routing import quota as _quota

    period = current_period()
    for model in candidates:
        pk, cost = _price(model)
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
            return reserve_credit(
                user, reservation_tokens,
                pricing_key=pk, cost_microusd=cost,
                quota_lines=quota_lines,
                quota_model=model if quota_lines else None,
                selected_model=model,
            )
        except QuotaExhausted as e:
            logger.info("quota_cascade_advance", tenant_id=user.org_id,
                        exhausted_model=e.model, period=period)
            continue
    logger.info("model_quota_all_exhausted", tenant_id=user.org_id, period=period)
    raise _err_402("model_quota_exhausted")


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
        if quota_lines:
            txn_items.extend(quota_lines)
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
            # *quota_lines(3..)]. A ConditionalCheckFailed at a QUOTA index means
            # the per-model quota is exhausted — NOT a snapshot race — so retrying
            # would fail forever. Surface QuotaExhausted so the caller's cascade
            # advances to the next model. (The pool/user snapshot indices 0-1 are
            # the retryable race; index 2 is the hold_id collision guard.)
            if quota_model is not None and len(reasons) > 3:
                for r in reasons[3:]:
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
    # real usage and the reservation's pricing key. This lets route handlers
    # settle a pooled request by passing only `context=ctx`.
    if (
        actual_cost_microusd is None
        and context is not None
        and context.pool_active
        and context.pricing_key
    ):
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
            _settle_pool_side(user, context, int(actual_cost_microusd))
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

    # ALWAYS record usage, even if the pool settle above failed: the Bedrock
    # call happened and its cost must be auditable. This is deliberately outside
    # any pool try/except so a settle fault cannot swallow the ledger entry.
    UsageLogsRepository().record(
        tenant_id=user.org_id,
        user_id=user.user_id,
        user_email=user.email,
        model_id=model_id,
        input_tokens=actual_input_tokens,
        output_tokens=actual_output_tokens,
        cost_microusd=actual_cost_microusd,
    )


def _settle_pool_side(user, context, actual_cost_microusd: int) -> None:
    """Move this reservation's `reserved` into `settled` and delete its hold in
    one transaction, with the double-subtract and vanished-row races handled.

    The transaction is [aggregate settle, conditional hold delete]. Three cancel
    outcomes are reconciled by inspecting the per-item CancellationReasons:
      - hold-delete (index 1) failed ConditionalCheckFailed → the reaper already
        reclaimed this hold AND already returned its reserved share, so we must
        NOT subtract reserved again; record settled-only instead;
      - aggregate (index 0) failed ConditionalCheckFailed → the pool row was
        deleted mid-flight (pool_vanished) → nothing to reconcile, no-op;
      - anything else → transient, retry with the same idempotency token.
    """
    budgets = TenantBudgetsRepository()
    # TransactItems ORDER IS A CONTRACT: index 0 = pool-row settle, index 1 =
    # hold delete. The cancellation-reason parsing below reads reasons[_POOL_IDX]
    # / reasons[_HOLD_IDX] by position, so these must stay in sync with the order
    # items are appended here. Do not reorder without updating both.
    _POOL_IDX = 0
    _HOLD_IDX = 1
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
        items.append(
            budgets.hold_delete_txn_item(tenant_id=user.org_id, sk=context.hold_sk)
        )
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
                hold_gone = (
                    len(reasons) > _HOLD_IDX and reasons[_HOLD_IDX] == "ConditionalCheckFailed"
                )
                row_gone = (
                    len(reasons) > _POOL_IDX and reasons[_POOL_IDX] == "ConditionalCheckFailed"
                )
                if hold_gone:
                    # Reaper already returned `reserved`; just record the spend.
                    logger.info(
                        "pool_settle_hold_already_reclaimed",
                        tenant_id=user.org_id,
                        period=context.period,
                        reserved_microusd=context.pool_reserved_microusd,
                        actual_microusd=actual_cost_microusd,
                    )
                    try:
                        client.transact_write_items(
                            TransactItems=[
                                _settled_only_txn_item(
                                    table_name=budgets.table_name,
                                    tenant_id=user.org_id,
                                    period=context.period,
                                    actual_microusd=actual_cost_microusd,
                                )
                            ],
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
                        # settled-only cancelled → pool row also gone; nothing to do.
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
