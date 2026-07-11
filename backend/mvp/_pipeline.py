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
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import boto3
from botocore.exceptions import ClientError
from fastapi import HTTPException

from core.logging import get_logger
from dynamo import UsageLogsRepository, UserTenantsRepository
from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
from dynamo.user_tenants import CreditExhaustedError


logger = get_logger(__name__)

# Reserving the pool touches a single hot item (the tenant's period row), so
# under contention the optimistic snapshot lock loses races. We retry more
# generously than the per-user path and back off a touch between attempts so a
# thundering herd does not exhaust the budget in microseconds. Crucially, when
# the retries ARE exhausted we fail *closed* (see reserve_credit): a pooled
# tenant must never have a request slip through unpriced just because the pool
# row was hot.
_RESERVE_MAX_RETRIES = 8
_RESERVE_BACKOFF_SECONDS = 0.01  # base; multiplied by the attempt index.
# Settlement must not fail a live request; it retries a few times against
# transient capacity errors before giving up loudly (a lost settle leaks the
# hold, so it is logged at error level for reconciliation).
_SETTLE_MAX_RETRIES = 4


def _pool_settle_items(
    *,
    table_name: str,
    tenant_id: str,
    period: str,
    reserved_microusd: int,
    actual_microusd: int,
):
    """Build the single TransactWriteItems fragment that settles a pool hold.

    Kept here (rather than inline) so both the settle and the error-path
    release compose the exact same update — moving `reserved` out of
    `pool_reserved` and `actual` into `pool_settled`.
    """
    from dynamo.tenant_budgets import budget_sk

    return {
        "Update": {
            "TableName": table_name,
            "Key": {
                "tenant_id": {"S": tenant_id},
                "sk": {"S": budget_sk(period)},
            },
            "UpdateExpression": (
                "ADD pool_reserved_microusd :dr, pool_settled_microusd :actual"
            ),
            "ExpressionAttributeValues": {
                ":dr": {"N": str(-int(reserved_microusd))},
                ":actual": {"N": str(int(actual_microusd))},
            },
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
        try:
            client = _low_level_client()
            client.transact_write_items(
                TransactItems=[
                    _pool_settle_items(
                        table_name=TenantBudgetsRepository().table_name,
                        tenant_id=self.tenant_id,
                        period=self.period,
                        reserved_microusd=self.pool_reserved_microusd,
                        actual_microusd=0,
                    )
                ],
                # Fresh per-call token: dedupes only botocore's transparent
                # retry of THIS release, never collides with a concurrent one.
                ClientRequestToken=_fresh_idempotency_token(),
            )
        except ClientError:
            logger.warning(
                "pool_release_failed",
                tenant_id=self.tenant_id,
                period=self.period,
                reserved_microusd=self.pool_reserved_microusd,
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


def reserve_credit_for_model(
    user,
    reservation_tokens: int,
    *,
    model_name: str,
    input_tokens_est: int,
    max_output_tokens: int,
    effort_multiplier: int = 1,
) -> ReservationContext:
    """Reserve credit for a request, pricing the pool debit from the model.

    Thin wrapper the route handlers call: resolves the model's `pricing_key`,
    estimates the dollar cost of the reservation, and delegates to
    `reserve_credit`. When the tenant has no pool budget this is exactly the
    per-user token reservation as before; the pricing work is cheap and only
    matters when a pool is present.
    """
    from .models import resolve_model
    from .pricing import estimate_cost_microusd

    try:
        pricing_key = resolve_model(model_name).pricing_key
    except ValueError:
        pricing_key = "default"
    cost = estimate_cost_microusd(
        pricing_key=pricing_key,
        input_tokens_est=input_tokens_est,
        max_output_tokens=max_output_tokens,
        effort_multiplier=effort_multiplier,
    )
    return reserve_credit(
        user,
        reservation_tokens,
        pricing_key=pricing_key,
        cost_microusd=cost,
    )


def reserve_credit(
    user,
    reservation_tokens: int,
    *,
    pricing_key: Optional[str] = None,
    cost_microusd: Optional[int] = None,
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

    # No pool budget → original single-table fast path (fully backward compat).
    if pool is None or cost_microusd is None:
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
    pool_vanished = False
    saw_throttle = False
    for _attempt in range(_RESERVE_MAX_RETRIES):
        if _attempt:
            # Small linear backoff so a thundering herd on one hot pool row does
            # not burn all retries in the same microsecond. Keeps the snapshot
            # lock viable under contention instead of collapsing into fail-open.
            time.sleep(_RESERVE_BACKOFF_SECONDS * _attempt)

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
        try:
            client.transact_write_items(
                TransactItems=[user_txn, pool_txn],
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
        )

    # Pool row deleted mid-flight → per-user-only reservation is correct.
    if pool_vanished:
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
        table_name = TenantBudgetsRepository().table_name
        item = _pool_settle_items(
            table_name=table_name,
            tenant_id=user.org_id,
            period=context.period,
            reserved_microusd=context.pool_reserved_microusd,
            actual_microusd=int(actual_cost_microusd),
        )
        # One fresh token for this settle, generated ONCE before the loop and
        # reused across our explicit retries: the settle params are timestamp-
        # free (no updated_at), so a retry after a lost ack carries the same
        # token+params and DynamoDB dedupes it to success instead of double-
        # applying. A fresh UUID keeps it distinct from any other request's
        # settle.
        token = _fresh_idempotency_token()
        client = _low_level_client()
        settled_ok = False
        for _attempt in range(_SETTLE_MAX_RETRIES):
            if _attempt:
                time.sleep(_RESERVE_BACKOFF_SECONDS * _attempt)
            try:
                client.transact_write_items(
                    TransactItems=[item], ClientRequestToken=token
                )
                settled_ok = True
                break
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                # A duplicate token means a prior identical call already applied
                # this exact settlement — treat as success, never re-apply.
                if code == "IdempotentParameterMismatchException":
                    settled_ok = True
                    break
                logger.warning(
                    "pool_settle_retry",
                    tenant_id=user.org_id,
                    period=context.period,
                    attempt=_attempt,
                    error_code=code,
                )
                continue
        if not settled_ok:
            # Do NOT swallow silently: an unsettled hold leaks pool budget until
            # an operator reconciles it. Emit a loud, structured record so the
            # leak is detectable and repairable (there is no reaper).
            logger.error(
                "pool_settle_failed",
                tenant_id=user.org_id,
                period=context.period,
                reserved_microusd=context.pool_reserved_microusd,
                actual_microusd=actual_cost_microusd,
            )

    UsageLogsRepository().record(
        tenant_id=user.org_id,
        user_id=user.user_id,
        user_email=user.email,
        model_id=model_id,
        input_tokens=actual_input_tokens,
        output_tokens=actual_output_tokens,
        cost_microusd=actual_cost_microusd,
    )
