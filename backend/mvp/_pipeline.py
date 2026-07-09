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
from dataclasses import dataclass
from typing import Optional

import boto3
from botocore.exceptions import ClientError
from fastapi import HTTPException

from core.logging import get_logger
from dynamo import UsageLogsRepository, UserTenantsRepository
from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
from dynamo.user_tenants import CreditExhaustedError


logger = get_logger(__name__)

_RESERVE_MAX_RETRIES = 5


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


def _low_level_client():
    region = os.getenv("AWS_REGION", "us-east-1")
    return boto3.client("dynamodb", region_name=region)


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
    for _attempt in range(_RESERVE_MAX_RETRIES):
        item = repo.get(user.user_id, user.org_id)
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

        pool_row = budgets.get(user.org_id, period)
        if pool_row is None:
            # Pool disappeared between the outer check and here — fall back to
            # per-user-only on the next loop by treating it as no-pool.
            break
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
            client.transact_write_items(TransactItems=[user_txn, pool_txn])
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code != "TransactionCanceledException":
                raise
            # A condition failed on one leg because a concurrent reserve/settle
            # moved a snapshot value. Neither leg committed (transactions are
            # all-or-nothing), so simply retry with fresh reads; the Python
            # ceiling checks above will raise the right 402 if we are now
            # genuinely exhausted.
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

    # Fell through the pool loop without committing: either the pool vanished
    # mid-flight, or we exhausted retries under contention. Do the per-user
    # reservation alone so the request is still correctly token-budgeted.
    try:
        repo.reserve(
            user_id=user.user_id, tenant_id=user.org_id, tokens=reservation_tokens
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
) -> None:
    """Settle the reservation against actual usage and write a UsageLogs row.

    Token side (always): refund the diff when actual <= reservation, or
    best-effort top-up + clamp with a `credit_overrun` warning when actual >
    reservation. UsageLogs always receives the true actual usage.

    Pool side (only when the reservation was pooled): move the reserved
    micro-USD out of `pool_reserved` and the actual micro-USD into
    `pool_settled` in one update, so the pool's outstanding reservation is
    released and real spend is recorded.

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
        )

    if (
        context is not None
        and context.pool_active
        and context.pool_reserved_microusd > 0
        and actual_cost_microusd is not None
        and context.period is not None
    ):
        try:
            budgets = TenantBudgetsRepository()
            budgets._table.update_item(  # noqa: SLF001 — same package, intentional.
                Key={"tenant_id": user.org_id, "sk": f"BUDGET#{context.period}"},
                UpdateExpression=(
                    "ADD pool_reserved_microusd :dr, pool_settled_microusd :actual"
                ),
                ExpressionAttributeValues={
                    ":dr": -int(context.pool_reserved_microusd),
                    ":actual": int(actual_cost_microusd),
                },
            )
        except ClientError:
            logger.warning(
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
    )
