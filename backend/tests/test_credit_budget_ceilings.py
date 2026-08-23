"""Ceilings accepted for credit budgets.

The previous bound on every token-credit field was 10_000_000. A single
coding-agent session spends on the order of 10^6 tokens, so an operator setting a
realistic budget hit `Input should be less than or equal to 10000000` and had no
way around it: the validation ceiling was smaller than ordinary usage.

These tests pin the current ceilings, and — more importantly — pin the invariant
that the request models all read them from one place. The bound used to be
written inline in eight request models plus ten frontend inputs, so raising it
anywhere left the rest silently rejecting values the API would accept.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from dynamo.user_tenants import UNLIMITED_CREDIT
from limits import MAX_POOL_BUDGET_USD_CENTS, MAX_TOKEN_CREDIT
from mvp.credit_ops import CreditAction
from mvp.admin_sso_invites import CreateSsoInviteRequest
from mvp.admin_trusted_accounts import (
    CreateTrustedAccountRequest,
    UpdateTrustedAccountRequest,
)
from mvp.admin_tenants import (
    CreateTenantRequest,
    SetPoolBudgetRequest,
    UpdateTenantRequest,
)
from mvp.admin_users import AssignTenantRequest, CreateUserRequest, SetCreditRequest
from mvp.team_lead import CreateTenantTeamLeadRequest, UpdateTenantTeamLeadRequest


def test_the_finite_ceiling_stays_below_the_unlimited_sentinel() -> None:
    """Raising the ceiling must not make the sentinel reachable as a plain value.

    `UNLIMITED_CREDIT` is a magic total that means "no cap". If the finite bound
    ever reached it, an operator typing that number would silently grant unlimited
    spend instead of a large budget — the failure would look like a successful
    request.
    """
    assert MAX_TOKEN_CREDIT < UNLIMITED_CREDIT
    with pytest.raises(ValidationError):
        CreditAction(total_credit=UNLIMITED_CREDIT)


def test_the_ceiling_survives_storage_and_serialization() -> None:
    """The raised value has to make it through the storage and wire layers.

    Credits are written as DynamoDB Numbers (Decimal) and compared against a used
    counter, and the API serializes them as JSON. 10^10 is well inside the range
    both handle exactly — this pins that rather than assuming it.
    """
    import json
    from decimal import Decimal

    stored = Decimal(MAX_TOKEN_CREDIT)
    assert int(stored) == MAX_TOKEN_CREDIT
    assert stored - Decimal(1) == Decimal(MAX_TOKEN_CREDIT - 1)
    # Well below JavaScript's Number.MAX_SAFE_INTEGER, so browsers reading the
    # value do not lose precision.
    assert MAX_TOKEN_CREDIT < 2**53
    assert json.loads(json.dumps({"total_credit": MAX_TOKEN_CREDIT}))["total_credit"] == (
        MAX_TOKEN_CREDIT
    )


def test_ceilings_are_above_a_realistic_agent_workload() -> None:
    # An afternoon of agent work is ~10^6 tokens; a monthly budget therefore has
    # to allow several orders of magnitude more than that.
    assert MAX_TOKEN_CREDIT >= 10_000_000_000
    # A tenant aggregates users, so its dollar ceiling must sit above any single
    # user's budget.
    assert MAX_POOL_BUDGET_USD_CENTS >= 1_000_000_000


def test_set_credit_accepts_the_ceiling_and_rejects_above_it() -> None:
    assert SetCreditRequest(total_credit=MAX_TOKEN_CREDIT).total_credit == MAX_TOKEN_CREDIT
    with pytest.raises(ValidationError):
        SetCreditRequest(total_credit=MAX_TOKEN_CREDIT + 1)
    # Zero stays legal: it is how an operator freezes a user.
    assert SetCreditRequest(total_credit=0).total_credit == 0
    with pytest.raises(ValidationError):
        SetCreditRequest(total_credit=-1)
    # The `unlimited` escape hatch is unaffected by the finite ceiling: it sets the
    # sentinel cap instead of a number, so both paths must keep working.
    assert SetCreditRequest(unlimited=True).unlimited is True


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (CreditAction, "total_credit"),
        (CreateUserRequest, "total_credit"),
        (AssignTenantRequest, "total_credit"),
        (SetCreditRequest, "total_credit"),
        (CreateSsoInviteRequest, "total_credit"),
        (CreateTenantRequest, "default_credit"),
        (UpdateTenantRequest, "default_credit"),
        (CreateTenantTeamLeadRequest, "default_credit"),
        (UpdateTenantTeamLeadRequest, "default_credit"),
        (CreateTrustedAccountRequest, "default_credit"),
        (UpdateTrustedAccountRequest, "default_credit"),
    ],
)
def test_every_credit_field_shares_one_ceiling(model: type, field: str) -> None:
    """No request model may carry its own, lower bound.

    Read the bound off the model rather than trusting the source text, so a
    hand-edited `le=` in any one of these fails here instead of in production.
    """
    bound = model.model_fields[field].metadata
    limits = [getattr(m, "le", None) for m in bound]
    assert MAX_TOKEN_CREDIT in limits, f"{model.__name__}.{field} does not use MAX_TOKEN_CREDIT"


def test_pool_budget_accepts_the_ceiling_and_rejects_above_it() -> None:
    ok = SetPoolBudgetRequest(limit_usd_cents=MAX_POOL_BUDGET_USD_CENTS)
    assert ok.limit_usd_cents == MAX_POOL_BUDGET_USD_CENTS
    with pytest.raises(ValidationError):
        SetPoolBudgetRequest(limit_usd_cents=MAX_POOL_BUDGET_USD_CENTS + 1)
