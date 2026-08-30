"""Declares which admission limits exist, and which callable enforces each.

The gateway enforces up to three limits at admission: the tenant dollar pool, the
per-user token quota, and the per-model quota. The admission decision is ONE
atomic `TransactWriteItems` (assembled in `mvp._pipeline`), and every configured
limit MUST contribute an item to it — a limit that is configured but contributes
no item is a silent bypass: the operator believes it is enforced and it is not.

This is the limits-side counterpart of `mvp.pricing.BILLABLE_LEGS`. There, a rate
column that charged money with no corresponding leg was a leg the reservation
bound could not see, and the two sides of the money path (charge, and bound) had
enumerated the columns separately and disagreed. Here the two sides are: a limit
kind existing at all, and a builder that turns its configured value into a
transaction item at RESERVE time. Declaring both halves of each kind in ONE place
means a fourth limit kind cannot be added — a config field to read it from,
without also naming the builder that makes it real — without the declaration and
the code drifting apart in a way `tests/test_reserve_limits_registry.py` can see.

`builder` is resolved eagerly from `module_name` + `builder_qualname` so a typo in
either fails at import time rather than silently returning the wrong callable.

This module is additive only: nothing here is imported by `mvp._pipeline` (or any
other assembly point) yet. Wiring it in — routing the three existing call sites
through `RESERVE_LIMITS` instead of naming `reserve_txn_item` / `hold_put_txn_item`
/ `build_reserve_txn_items` directly — is a follow-up change to files this module
does not own.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Callable


def _resolve(module_name: str, qualname: str) -> Callable[..., Any]:
    """`qualname` is `"function_name"` for a module-level function, or
    `"ClassName.method_name"` for an instance method looked up on the class
    (i.e. the plain unbound function, not a bound method of any instance)."""
    obj: Any = importlib.import_module(module_name)
    for part in qualname.split("."):
        obj = getattr(obj, part)
    return obj


@dataclass(frozen=True)
class LimitKind:
    """One admission limit: its identity, where its configured value is read
    from, and the callable that turns that value into a RESERVE-time
    transaction item. `module_name` + `builder_qualname` name WHERE the
    builder lives, so a discovery sweep over that module can confirm this is
    the only builder there, not just that this one exists."""

    name: str
    config_source: str
    module_name: str
    builder_qualname: str
    builder: Callable[..., Any]


def _limit(name: str, config_source: str, module_name: str, builder_qualname: str) -> LimitKind:
    return LimitKind(
        name=name,
        config_source=config_source,
        module_name=module_name,
        builder_qualname=builder_qualname,
        builder=_resolve(module_name, builder_qualname),
    )


#: ONE definition of the limits the admission transaction enforces. Read by the
#: closure test (`tests/test_reserve_limits_registry.py`), which fails the build
#: if a limit kind is declared with no reachable builder, or a RESERVE-direction
#: transaction-item builder exists in one of these modules with no declared kind.
RESERVE_LIMITS: tuple[LimitKind, ...] = (
    _limit(
        name="tenant_dollar_pool",
        config_source=(
            "dynamo.tenant_budgets: the BUDGET#<period> row's pool_limit_microusd, "
            "set by TenantBudgetsRepository.set_pool_limit"
        ),
        module_name="dynamo.tenant_budgets",
        builder_qualname="TenantBudgetsRepository.reserve_txn_item",
    ),
    _limit(
        name="user_token_quota",
        config_source=(
            "dynamo.user_tenants: the user row's total_credit, set via "
            "UserTenantsRepository / mvp.credit_ops.CreditAction"
        ),
        module_name="dynamo.user_tenants",
        builder_qualname="UserTenantsRepository.reserve_txn_item",
    ),
    _limit(
        name="per_model_quota",
        config_source=(
            "mvp.routing.model_resolver.ModelQuotaConfig.limit (tenant- and/or "
            "user-scoped), read via mvp.routing.config"
        ),
        module_name="mvp.routing.quota",
        builder_qualname="build_reserve_txn_items",
    ),
)


#: The modules a RESERVE-direction limit builder can live in today. Fixed and
#: NOT derived from `RESERVE_LIMITS` on purpose: if it were derived (e.g. "every
#: module a declared kind names"), then deleting a kind's declaration would also
#: delete its module from the swept set, and the closure test's strong direction
#: would stop checking that module entirely instead of flagging the now-orphaned
#: builder still sitting there. Kept in sync with "WHERE THE THREE LIMITS LIVE"
#: by hand — this is the one place in the design that IS a hand-list, and it is
#: a list of modules to look in, not of the builders to find, which is the
#: distinction `tests/test_reserve_limits_registry.py`'s docstring draws.
KNOWN_LIMIT_MODULES: tuple[str, ...] = (
    "dynamo.tenant_budgets",
    "dynamo.user_tenants",
    "mvp.routing.quota",
)


def limit_kinds_in_module(module_name: str) -> tuple[LimitKind, ...]:
    return tuple(k for k in RESERVE_LIMITS if k.module_name == module_name)
