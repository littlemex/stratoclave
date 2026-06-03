"""Shared credit-reservation pipeline for the Anthropic Messages route and
the OpenAI Responses route.

Both routes share the exact same money-flow:

    1. authenticate the caller
    2. resolve and allowlist the model
    3. estimate a reservation (input + max_output) and atomically debit it
    4. invoke Bedrock
    5. settle the reservation against the actual usage and append a
       UsageLogs row

Steps 3 and 5 are protocol-agnostic: they touch only `UserTenants` and
`UsageLogs` tables, and operate on integer token counts. They live here so
the two route handlers cannot drift in their credit semantics.

Each route owns its own minimum-reservation floor (`anthropic.py` uses
1024; `openai_responses.py` uses 8192). Those policies are intentionally
**not** shared from this module — different defaults reflect different
typical workloads (Claude messages tend to be short; codex/GPT-5 reasoning
runs are large) and merging them under a single name would silently
constrain one or relax the other.
"""
from __future__ import annotations

from fastapi import HTTPException

from core.logging import get_logger
from dynamo import UsageLogsRepository, UserTenantsRepository
from dynamo.user_tenants import CreditExhaustedError

from .deps import AuthenticatedUser


logger = get_logger(__name__)


def reserve_credit(
    user: AuthenticatedUser, reservation_tokens: int
) -> UserTenantsRepository:
    """Atomically debit `reservation_tokens` from the caller's balance.

    Returns the `UserTenantsRepository` so the caller can refund / re-reserve
    on the same instance after Bedrock returns. Raises HTTP 402 with the
    Anthropic-style `credit_exhausted` payload if the balance is too low —
    both routes return the same error shape so SDK clients have one path
    to handle.
    """
    repo = UserTenantsRepository()
    repo.ensure(user_id=user.user_id, tenant_id=user.org_id)
    try:
        repo.reserve(
            user_id=user.user_id,
            tenant_id=user.org_id,
            tokens=reservation_tokens,
        )
    except CreditExhaustedError:
        remaining = repo.remaining_credit(user.user_id, user.org_id)
        raise HTTPException(
            status_code=402,
            detail={
                "type": "credit_exhausted",
                "message": (
                    "Insufficient credit balance for this request. "
                    "Contact your admin."
                ),
                "remaining_credit": remaining,
                "reservation_required": reservation_tokens,
            },
        )
    return repo


def settle_reservation_and_log(
    *,
    user: AuthenticatedUser,
    tenants_repo: UserTenantsRepository,
    reservation: int,
    actual_input_tokens: int,
    actual_output_tokens: int,
    model_id: str,
) -> None:
    """Settle the reservation against actual usage and write a UsageLogs row.

    - actual <= reservation → refund the diff
    - actual > reservation → best-effort top-up via an additional reserve;
      if the user is now out of credit, clamp credit_used to total_credit
      and emit a `credit_overrun` warning. UsageLogs always receives the
      true actual usage so audit trails never lie.

    Exceptions from the Dynamo layer are propagated to the caller; this
    function never silently swallows them.
    """
    actual = max(actual_input_tokens + actual_output_tokens, 0)
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
                        # Lost a race with a concurrent debit. The clamped
                        # amount stays as `uncovered` for the audit log.
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

    UsageLogsRepository().record(
        tenant_id=user.org_id,
        user_id=user.user_id,
        user_email=user.email,
        model_id=model_id,
        input_tokens=actual_input_tokens,
        output_tokens=actual_output_tokens,
    )
