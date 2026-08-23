"""Validation ceilings for the API request models.

These are API-layer constraints, not storage concerns, so they live here rather
than beside the DynamoDB repositories: `mvp/` should not have to import from
`dynamo/` to know what a request may contain.

They are also deliberately in one place. Each bound used to be written inline in
every request model and every frontend form, so raising it anywhere left the rest
rejecting values the API would accept.
"""

from __future__ import annotations

# Upper bound accepted for a token credit budget (a user's balance, and the
# per-user default a tenant hands to its members).
#
# The previous ceiling was 10_000_000. A single coding-agent session spends on the
# order of 10^6 tokens, so an operator granting a realistic monthly budget hit a
# validation error with no way around it: the ceiling was smaller than ordinary
# usage. It stays finite rather than becoming unbounded — the value is stored as a
# DynamoDB Number and compared against a used counter, and
# `dynamo.user_tenants.UNLIMITED_CREDIT` already exists for the uncapped case, so
# this bound must remain below that sentinel (asserted in
# tests/test_credit_budget_ceilings.py).
MAX_TOKEN_CREDIT = 10_000_000_000

# Upper bound accepted for a tenant's dollar pool ceiling, in whole USD cents.
# A tenant aggregates many users, so its ceiling has to sit well above any single
# user's budget; $10,000,000 per period leaves room for that without accepting
# arbitrary integers.
MAX_POOL_BUDGET_USD_CENTS = 1_000_000_000
