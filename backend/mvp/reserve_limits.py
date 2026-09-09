"""Declares which admission limits exist, and which callable enforces each.

The gateway enforces up to four limits at admission: the tenant dollar pool,
the per-user token quota, the per-model quota, and the per-user money ceiling
(P3.1). The admission decision is ONE
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

This module IS imported: `mvp/grants.py:92` reads `RESERVE_LIMITS` and
`is_grantable_wall` from here (P3.7) — the four RESERVE call sites in
`mvp._pipeline` (including P3.1's new one) still name their builders directly
rather than routing through this registry, and wiring that in remains a
follow-up change to files this module does not own, but the registry itself
is no longer inert.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Callable

#: What a wall's `configured_when` predicate is handed. Each wall defines what
#: its own snapshot means (a raw DynamoDB item, a resolved value, a tuple —
#: whatever that ONE read produced); this registry never inspects it, so `Any`
#: rather than a shared shape every wall would have to agree on for a thing
#: only two functions (the predicate and the builder) ever look at (I5).
ConfigSnapshot = Any


def _always_configured(_snapshot: ConfigSnapshot) -> bool:
    """The default `configured_when`: this wall always contributes when its
    caller decides to ask it to. The three walls PR 3 did not touch
    (`tenant_dollar_pool`, `user_token_quota`, `per_model_quota`) each decide
    whether to build an item from their OWN config lookup already, at their
    OWN call sites, before this registry enters the picture — so giving them a
    real predicate here would be a SECOND, redundant decision point, exactly
    the failure I5 exists to prevent. Only a wall whose "configured or not"
    question the admission path itself must answer through this registry
    (`user_dollar_quota`, P3.6) needs a non-trivial one."""
    return True


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
    #: Can a refusal at this wall be answered by asking a person for more?
    #:
    #: Declared per kind rather than decided at the refusal, because "is this
    #: grantable" is a property of the limit and not of the request that hit it --
    #: and a refusal path that worked it out for itself would be a second place
    #: the answer lives. Exactly one wall is grantable today, and the two that are
    #: not include one denominated in money: being micro-USD does not make a limit
    #: raisable, and that is precisely the mistake this field exists to stop
    #: somebody making at the call site.
    grantable: bool = False
    #: Is this wall configured for the (tenant, period, ...) the admission
    #: path is about to check, given the ONE snapshot it already read? (P3.6)
    #:
    #: Defaults to "always" (`_always_configured`) because the three existing
    #: walls each already gate their own builder call on their own config
    #: lookup, at their own call site, before this registry is consulted --
    #: a second predicate here would be a second decision point for the exact
    #: race I5 exists to prevent. A wall whose builder needs to be handed the
    #: caller's own resolved snapshot (`user_dollar_quota`, whose base can be
    #: absent) declares a real one instead.
    configured_when: Callable[[ConfigSnapshot], bool] = _always_configured


def _limit(
    name: str, config_source: str, module_name: str, builder_qualname: str,
    *, grantable: bool = False,
    configured_when: Callable[[ConfigSnapshot], bool] = _always_configured,
) -> LimitKind:
    return LimitKind(
        name=name,
        config_source=config_source,
        module_name=module_name,
        builder_qualname=builder_qualname,
        builder=_resolve(module_name, builder_qualname),
        configured_when=configured_when,
        grantable=grantable,
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
            "which is baseline + coalesce(pool_granted, 0), moved by "
            "TenantBudgetsRepository.set_manual_limit / clear_manual_limit / "
            "adjust_pool_for_seat_delta"
        ),
        module_name="dynamo.tenant_budgets",
        builder_qualname="TenantBudgetsRepository.reserve_txn_item",
        # The ONE raisable wall. A tenant refused here can ask an approver for a
        # grant, which moves `pool_granted_microusd` for a bounded window.
        grantable=True,
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
    _limit(
        name="user_dollar_quota",
        config_source=(
            "dynamo.tenants: the tenant row's user_dollar_defaults (period-keyed) "
            "resolved and SEALED per period by "
            "TenantsRepository.seal_user_dollar_base, set via "
            "TenantsRepository.set_user_dollar_default (P3.2/P3.3)"
        ),
        module_name="mvp.routing.user_dollar_quota",
        builder_qualname="build_reserve_txn_items",
        # Money-denominated but NOT raisable (P3.6): being micro-USD does not
        # make a limit grantable, and PR 3 ships no raise path, flip, or slot
        # for this wall at all (O3.1) -- grantable=True here would be a
        # promise the raise endpoint (`mvp.grants.submit_limit_raise`) cannot
        # keep, since it accepts only `POOL_WALL`.
        grantable=False,
        configured_when=_resolve(
            "mvp.routing.user_dollar_quota", "configured_when"),
    ),
)


#: The modules a RESERVE-direction limit builder can live in today. Fixed and
#: NOT derived from `RESERVE_LIMITS` on purpose: if it were derived (e.g. "every
#: module a declared kind names"), then deleting a kind's declaration would also
#: delete its module from the swept set, and the closure test's strong direction
#: would stop checking that module entirely instead of flagging the now-orphaned
#: builder still sitting there. Kept in sync with the four limits' own
#: modules by hand — this is the one place in the design that IS a hand-list,
#: and it is a list of modules to look in, not of the builders to find, which
#: is the distinction `tests/test_reserve_limits_registry.py`'s docstring draws.
#: (A count beside this list is the stale-number defect this change has
#: already fixed twice, so the prose says "these four" rather than restating
#: a number a fifth entry would make wrong again.)
KNOWN_LIMIT_MODULES: tuple[str, ...] = (
    "dynamo.tenant_budgets",
    "dynamo.user_tenants",
    "mvp.routing.quota",
    "mvp.routing.user_dollar_quota",
)


def limit_kinds_in_module(module_name: str) -> tuple[LimitKind, ...]:
    return tuple(k for k in RESERVE_LIMITS if k.module_name == module_name)


def limit_kind(name: str) -> LimitKind:
    """The declared kind called `name`. Raises rather than returning None: every
    caller here is asking about a wall a refusal just named, and a refusal naming
    a wall this registry has never heard of is a bug in the refusal."""
    for kind in RESERVE_LIMITS:
        if kind.name == name:
            return kind
    raise KeyError(
        f"{name!r} is not a declared admission limit. The declared ones are "
        f"{sorted(k.name for k in RESERVE_LIMITS)}; a refusal that names another "
        f"wall is describing a limit nothing enforces.")


def is_grantable_wall(name: str) -> bool:
    """Can a refusal at `name` be answered by asking for a raise?

    Reads the declaration rather than testing the name against a literal, so the
    refusal path and the raise path cannot disagree about which wall is which.
    """
    return limit_kind(name).grantable


def wall_names() -> frozenset[str]:
    """Every wall a refusal may name, derived from the declaration."""
    return frozenset(k.name for k in RESERVE_LIMITS)
