"""The closed-world declaration of the tenant pool row.

Four separate areas need to know what is on this row: the period rollover in
`dynamo.tenant_budgets` (what to carry across a month boundary), the reconciler
under `mvp.observability` (what to compare against a source), the document test
over `CONTRACTS.md` (who writes the ceiling), and the size guard in `iac` and
`bench` (how wide the item can get). Each of those held its own list, and a list
is forgotten silently -- an attribute added later evaporates at the next
rollover, or is checked by nobody, or is missing from the measured item, and
nothing says so.

So this is not an appendable list. It is a TOTAL CLASSIFICATION: every attribute
a pool row can carry appears here exactly once, with its rollover class, its
writers, either a covering reconciler check or an explicit exemption, and the
widest value it can hold. An attribute found on a row and absent from here is a
failure, which is what makes forgetting impossible rather than merely unlikely.

WHY IT IS A MAPPING AND NOT A SEQUENCE. Two reasons, and the second is the one
that matters. Every consumer either looks an attribute up by name or walks all of
them, and the closed-world check's central operation is "is this key declared?" --
which is a mapping's primitive and, for a sequence, an index it has to build first.
And a mapping keyed by name makes TWO ENTRIES FOR ONE ATTRIBUTE unrepresentable. A
sequence lets a careless append declare `seat_count` twice with different rollover
classes, and then the rollover carries it while the size accounting counts it twice
and neither reading is wrong on its own. The declaration's entire value is being the
single source for this row, so a container that can hold a contradiction about it is
the wrong container.

WHY THIS IS ITS OWN MODULE, and it is load-bearing rather than tidiness. If the
declaration lived inside `tenant_budgets.py` and the size guard read a second
copy from somewhere else, the guard would be measuring a row shape the rollover
and the reconciler do not use -- and it could pass while the real row had grown,
which is the precise failure the declaration exists to make impossible. There is
ONE authority for the row's shape and this file is it. There is deliberately no
re-export from `tenant_budgets`: an alias would recreate the second authority,
and it would be a writer of the ceiling that `ceiling_writers()` cannot see.

It is also the right side of the choice for two smaller reasons. A module four
areas consume should not sit inside the largest of them, or an observability
module has to import a whole repository layer to read a tuple. And every other
module in `dynamo/` is named for an entity and exports a repository; a schema
declaration is neither, so a distinct module avoids implying it is one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


# The name of the operator's own figure. Its PRESENCE is the whole mode switch,
# so it is referenced by name in four places (the two writers, the rollover and
# the reconciler) and never spelled inline in any of them.
MANUAL_LIMIT_ATTR = "manual_limit_microusd"
SEAT_COUNT_ATTR = "seat_count"
SEAT_RATE_ATTR = "seat_rate_microusd"
POOL_GRANTED_ATTR = "pool_granted_microusd"


# ---------------------------------------------------------------------------
# Four separate mechanisms need to know what is on this row: the period rollover
# (what to carry), the ceiling-writer document test (who writes the ceiling), the
# reconciler (what to check against its source), and the item-size gauge (how
# wide the row can get). Each of those was a list, and a list is forgotten
# silently -- an attribute added later evaporates at the next rollover, or is
# checked by nobody, or is missing from the measured item, and nothing says so.
#
# So this is not an appendable list. It is a TOTAL CLASSIFICATION: every
# attribute a pool row can carry appears here exactly once, with its rollover
# class, its writers, either a covering reconciler check or an explicit
# exemption, and the widest value it can hold. An attribute found on a row and
# absent from here is a failure, which is what makes forgetting impossible
# instead of merely unlikely.

ROLLOVER_CARRIED = "carried"    # copied verbatim into the new period's row
ROLLOVER_DERIVED = "derived"    # recomputed on the new row from carried attributes
ROLLOVER_RESET = "reset"        # not carried; the new period starts without it

# HOW a reset attribute is reset, which is a decision and not a detail.
RESET_BY_OMISSION = "omission"  # left off the new row entirely; absence is its zero
RESET_BY_ZERO = "zero"          # written as 0, because absence would mean something else


@dataclass(frozen=True)
class PoolAttribute:
    """One attribute of the pool row, in every class that has to know about it."""

    name: str
    rollover: str
    #: Every code site that writes this attribute, as `module:function`. The
    #: ceiling-writer document test derives its list from the subset of these
    #: whose attribute moves the ceiling, so the document cannot name a subset.
    writers: tuple[str, ...]
    #: Widest value this attribute can hold, in bytes as DynamoDB accounts for
    #: it (a number is its digit count, plus one for a sign). The item-size
    #: gauge and its alarm threshold are derived from the sum of these.
    max_value_bytes: int
    #: The reconciler check that compares this attribute to its SOURCE, or None
    #: with `exemption` saying why no source comparison exists for it.
    check: Optional[str] = None
    exemption: Optional[str] = None
    #: Only for `ROLLOVER_RESET`.
    reset_by: Optional[str] = None
    note: str = ""

    def __post_init__(self) -> None:
        if self.rollover not in (ROLLOVER_CARRIED, ROLLOVER_DERIVED, ROLLOVER_RESET):
            raise ValueError(f"{self.name}: unknown rollover class {self.rollover!r}")
        if self.rollover == ROLLOVER_RESET and self.reset_by not in (
                RESET_BY_OMISSION, RESET_BY_ZERO):
            raise ValueError(
                f"{self.name}: a reset attribute must say HOW it is reset "
                f"(omission or zero), got {self.reset_by!r}")
        if self.rollover != ROLLOVER_RESET and self.reset_by is not None:
            raise ValueError(f"{self.name}: reset_by is meaningless unless reset")
        if bool(self.check) == bool(self.exemption):
            raise ValueError(
                f"{self.name}: exactly one of check / exemption, so an attribute "
                f"nobody reconciles has to say so out loud")
        if self.max_value_bytes <= 0:
            raise ValueError(f"{self.name}: max_value_bytes must be positive")


# The widest micro-USD figure any ceiling attribute can hold: L8's maximum pool,
# in micro-USD. Written as a width rather than a value because the size gauge
# wants bytes and the digits are what DynamoDB charges for.
_MAX_POOL_MICROUSD_DIGITS = 16   # 1_000_000_000 cents x 10_000 = 1e13, 14 digits; +2 margin
_SIGNED = 1                      # headroom is the one signed money attribute

#: The one declaration, KEYED BY ATTRIBUTE NAME. F2 adds its grant cap here, in the
#: same shape, or the closed-world test fails at F2's merge -- loudly, and at the
#: moment the attribute appears, rather than silently on the 1st of the following
#: month.
#:
#: A mapping rather than a sequence, for the reason in the module docstring: a
#: sequence lets a careless append declare one attribute twice, in two different
#: classes, and the declaration's entire value is being the single source. Here that
#: is unrepresentable.
POOL_ROW_ATTRIBUTES: dict[str, PoolAttribute] = {
    "tenant_id": PoolAttribute(
        name="tenant_id",
        rollover=ROLLOVER_CARRIED,
        writers=("dynamo.tenant_budgets:TenantBudgetsRepository._seed_pool_row",),
        max_value_bytes=128,
        exemption="the partition key; it identifies the row rather than describing it",
    ),
    "sk": PoolAttribute(
        name="sk",
        rollover=ROLLOVER_DERIVED,
        writers=("dynamo.tenant_budgets:TenantBudgetsRepository._seed_pool_row",),
        max_value_bytes=32,
        exemption="the sort key; the new period's row has the new period in it by "
                  "construction, so carrying it verbatim would be the bug",
    ),
    MANUAL_LIMIT_ATTR: PoolAttribute(
        name=MANUAL_LIMIT_ATTR,
        rollover=ROLLOVER_CARRIED,
        writers=(
            "dynamo.tenant_budgets:TenantBudgetsRepository.set_manual_limit",
            "dynamo.tenant_budgets:TenantBudgetsRepository.clear_manual_limit",
            "dynamo.tenant_budgets:TenantBudgetsRepository._seed_pool_row",
            "migrations.pool_ceiling_migration:phase_m2_backfill",
        ),
        max_value_bytes=_MAX_POOL_MICROUSD_DIGITS,
        check="limit_identity",
        note="carried, and carried by ABSENCE too: a seat-tracked row must reach the "
             "new period still seat-tracked, so the rollover writes this attribute "
             "only when the old row had it. Zero is a figure and is carried as one.",
    ),
    SEAT_COUNT_ATTR: PoolAttribute(
        name=SEAT_COUNT_ATTR,
        rollover=ROLLOVER_CARRIED,
        writers=(
            "dynamo.tenant_budgets:TenantBudgetsRepository.adjust_pool_for_seat_delta",
            "dynamo.tenant_budgets:TenantBudgetsRepository._seed_pool_row",
            "migrations.pool_ceiling_migration:phase_m2_backfill",
        ),
        max_value_bytes=8,
        check="seat_count_matches_membership",
        note="the seats do not reset at a period boundary; the people are still there",
    ),
    SEAT_RATE_ATTR: PoolAttribute(
        name=SEAT_RATE_ATTR,
        rollover=ROLLOVER_CARRIED,
        writers=(
            "dynamo.tenant_budgets:TenantBudgetsRepository._seed_pool_row",
            "migrations.pool_ceiling_migration:phase_m1_add_attributes",
            "migrations.pool_ceiling_migration:recompute_seat_tracked_rows",
        ),
        max_value_bytes=12,
        check="seat_rate_matches_rate_in_force",
        note="carried so a ceiling is reproducible; changing it is a migration, not a "
             "deploy, which is what makes the boot-time refusal honest",
    ),
    POOL_GRANTED_ATTR: PoolAttribute(
        name=POOL_GRANTED_ATTR,
        rollover=ROLLOVER_RESET,
        reset_by=RESET_BY_OMISSION,
        writers=("(F2: the grant apply and revoke writers)",),
        max_value_bytes=_MAX_POOL_MICROUSD_DIGITS,
        exemption="no writer exists until F2, which registers the grant-sum check "
                  "against its own ACTIVE grants at that point",
        note="RESET BY OMISSION, never by writing 0: absence and zero mean the same "
             "thing here, which is exactly what is NOT true of manual_limit. Safe to "
             "reset at the boundary ONLY because F2 pins a grant's expires_at to at "
             "most the period end; without that pin this reset destroys live granted "
             "capacity every 1st.",
    ),
    "pool_limit_microusd": PoolAttribute(
        name="pool_limit_microusd",
        rollover=ROLLOVER_DERIVED,
        writers=(
            "dynamo.tenant_budgets:TenantBudgetsRepository.set_manual_limit",
            "dynamo.tenant_budgets:TenantBudgetsRepository.clear_manual_limit",
            "dynamo.tenant_budgets:TenantBudgetsRepository.adjust_pool_for_seat_delta",
            "dynamo.tenant_budgets:TenantBudgetsRepository._seed_pool_row",
        ),
        max_value_bytes=_MAX_POOL_MICROUSD_DIGITS,
        check="limit_identity",
        note="recomputed on the new row from the carried attributes; carrying last "
             "month's number would carry a granted term the new row does not have",
    ),
    "pool_headroom_microusd": PoolAttribute(
        name="pool_headroom_microusd",
        rollover=ROLLOVER_DERIVED,
        writers=(
            "dynamo.tenant_budgets:TenantBudgetsRepository.set_manual_limit",
            "dynamo.tenant_budgets:TenantBudgetsRepository.clear_manual_limit",
            "dynamo.tenant_budgets:TenantBudgetsRepository.adjust_pool_for_seat_delta",
            "dynamo.tenant_budgets:TenantBudgetsRepository._seed_pool_row",
            "dynamo.tenant_budgets:TenantBudgetsRepository.reconcile_headroom",
            "dynamo.tenant_budgets:TenantBudgetsRepository.reserve_txn_item",
            "dynamo.tenant_budgets:TenantBudgetsRepository.settle_txn_item",
            "dynamo.tenant_budgets:TenantBudgetsRepository.reserve_commit_txn_items",
            "dynamo.tenant_budgets:TenantBudgetsRepository.pool_credit_back",
        ),
        max_value_bytes=_MAX_POOL_MICROUSD_DIGITS + _SIGNED,
        check="headroom_identity",
        note="the one signed money attribute: an over-ceiling row's deficit is a "
             "figure an operator needs, and clamping it at zero hides the amount by "
             "which admission has already been exceeded",
    ),
    "pool_reserved_microusd": PoolAttribute(
        name="pool_reserved_microusd",
        rollover=ROLLOVER_RESET,
        reset_by=RESET_BY_ZERO,
        writers=(
            "dynamo.tenant_budgets:TenantBudgetsRepository._seed_pool_row",
            "dynamo.tenant_budgets:TenantBudgetsRepository.reserve_txn_item",
            "dynamo.tenant_budgets:TenantBudgetsRepository.settle_txn_item",
            "dynamo.tenant_budgets:TenantBudgetsRepository.reserve_commit_txn_items",
            "dynamo.tenant_budgets:TenantBudgetsRepository.pool_credit_back",
        ),
        max_value_bytes=_MAX_POOL_MICROUSD_DIGITS + _SIGNED,
        exemption="reconciled against the credit ledger, not against a row-side "
                  "source: mvp.admin_tenants.get_pool_reconciliation owns that axis",
    ),
    "pool_settled_microusd": PoolAttribute(
        name="pool_settled_microusd",
        rollover=ROLLOVER_RESET,
        reset_by=RESET_BY_ZERO,
        writers=(
            "dynamo.tenant_budgets:TenantBudgetsRepository._seed_pool_row",
            "dynamo.tenant_budgets:TenantBudgetsRepository.settle_txn_item",
        ),
        max_value_bytes=_MAX_POOL_MICROUSD_DIGITS,
        exemption="reconciled against the credit ledger; same owner as reserved",
    ),
    "pool_reclaimed_microusd": PoolAttribute(
        name="pool_reclaimed_microusd",
        rollover=ROLLOVER_RESET,
        reset_by=RESET_BY_ZERO,
        writers=("mvp._pipeline:_reclaim_expired_holds",),
        max_value_bytes=_MAX_POOL_MICROUSD_DIGITS,
        exemption="reconciled against the credit ledger; same owner as reserved",
    ),
    "status": PoolAttribute(
        name="status",
        rollover=ROLLOVER_CARRIED,
        writers=(
            "dynamo.tenant_budgets:TenantBudgetsRepository.set_manual_limit",
            "dynamo.tenant_budgets:TenantBudgetsRepository._seed_pool_row",
        ),
        max_value_bytes=16,
        exemption="not a quantity; a suspended pool stays suspended across a boundary "
                  "because suspension is an operator's decision, not a month's",
    ),
    "version": PoolAttribute(
        name="version",
        rollover=ROLLOVER_DERIVED,
        writers=("(every writer in this module stamps it)",),
        max_value_bytes=4,
        exemption="a schema marker; the new row is stamped at the current version",
    ),
    "updated_at": PoolAttribute(
        name="updated_at",
        rollover=ROLLOVER_DERIVED,
        writers=("(every writer in this module stamps it)",),
        max_value_bytes=40,
        exemption="a timestamp; carrying the old row's would misdate the new one",
    ),
    "sizing": PoolAttribute(
        name="sizing",
        rollover=ROLLOVER_RESET,
        reset_by=RESET_BY_OMISSION,
        writers=("(none: no writer remains; M4 deletes the attribute)",),
        max_value_bytes=8,
        exemption="the mode this change replaced. Declared, not removed from the "
                  "declaration, because rows still carry it until M4 runs and a "
                  "closed-world test must not fail on a row mid-migration. Nothing "
                  "reads it and the rollover never carries it forward.",
    ),
}

# The key IS the attribute name, and the spec repeats it so a spec read on its own
# can still say what it describes. Two places holding one string is two places to
# disagree, so they are checked against each other at import: a copy-paste that
# changes the key and not the name would otherwise declare a class for an attribute
# nothing on the row is called, and leave the real one unclassified.
for _key, _spec in POOL_ROW_ATTRIBUTES.items():
    if _key != _spec.name:
        raise RuntimeError(
            f"POOL_ROW_ATTRIBUTES is keyed {_key!r} but its spec is named "
            f"{_spec.name!r}; the key is the attribute name and nothing else")
del _key, _spec

#: The attributes that MOVE THE CEILING, derived from the declaration rather than
#: restated. The ceiling-writer document test reads this, so a writer added to
#: `pool_limit_microusd` above appears in the document's obligation immediately
#: and a hardcoded list cannot go green while naming a subset.
CEILING_ATTRS: tuple[str, ...] = (
    "pool_limit_microusd", "pool_headroom_microusd", MANUAL_LIMIT_ATTR,
    SEAT_COUNT_ATTR, SEAT_RATE_ATTR, POOL_GRANTED_ATTR,
)


def pool_attribute(name: str) -> Optional[PoolAttribute]:
    """The declaration for `name`, or None if it is in no class."""
    return POOL_ROW_ATTRIBUTES.get(name)


def unclassified_pool_attributes(item: dict[str, Any]) -> set[str]:
    """Every attribute on `item` that appears in no class of the declaration.

    The closed-world assertion, and it is a single mapping lookup per attribute --
    "is this key declared?" is the declaration's primitive rather than a scan it has
    to build an index for.

    A non-empty result is a failure and not a warning: it means something writes the
    pool row that four other mechanisms do not know exists.
    """
    return {str(k) for k in (item or {}) if str(k) not in POOL_ROW_ATTRIBUTES}


def ceiling_writers() -> tuple[str, ...]:
    """Every code site that writes a ceiling-bearing attribute, from the
    declaration. Derived, never listed: a literal list passes while naming a
    subset the moment a writer is added."""
    out: list[str] = []
    for name, attr in POOL_ROW_ATTRIBUTES.items():
        if name not in CEILING_ATTRS:
            continue
        for w in attr.writers:
            if not w.startswith("(") and w not in out:
                out.append(w)
    return tuple(out)


def carried_attributes() -> tuple[str, ...]:
    """The attributes the period rollover copies verbatim."""
    return tuple(name for name, attr in POOL_ROW_ATTRIBUTES.items()
                 if attr.rollover == ROLLOVER_CARRIED and name != "tenant_id")


def _assert_money_width_covers_the_maximum() -> None:
    """The declared money width is a constant, so it is CHECKED against the ceiling
    it has to cover rather than trusted.

    `_MAX_POOL_MICROUSD_DIGITS` is written out because the size accounting wants a
    number of bytes, not a value. A constant nobody checks is a constant that goes
    stale the first time `MAX_POOL_BUDGET_USD_CENTS` moves, and the symptom would be
    a size bound quietly too small — so the alarm derived from it would fire on a
    legal row. The import is local because `dynamo/` deliberately does not depend on
    the API-layer validation module (see `limits.py`'s own docstring).
    """
    from limits import MAX_POOL_BUDGET_USD_CENTS

    needed = len(str(int(MAX_POOL_BUDGET_USD_CENTS) * 10_000))
    if _MAX_POOL_MICROUSD_DIGITS < needed:
        raise RuntimeError(
            f"_MAX_POOL_MICROUSD_DIGITS is {_MAX_POOL_MICROUSD_DIGITS} but "
            f"MAX_POOL_BUDGET_USD_CENTS={MAX_POOL_BUDGET_USD_CENTS} needs {needed} "
            f"digits in micro-USD; the declared row width would under-count and the "
            f"size alarm derived from it would fire on a legal row")


def worst_case_pool_item_bytes() -> int:
    """The widest a pool row can get, from the declaration: every attribute's
    name plus its widest value. The item-size gauge's baseline and its alarm
    threshold are derived from this, so a schema change moves the alarm with it
    instead of failing it."""
    _assert_money_width_covers_the_maximum()
    return sum(len(name) + attr.max_value_bytes
               for name, attr in POOL_ROW_ATTRIBUTES.items() if name != "sizing")




class UndeclaredPoolAttributeError(RuntimeError):
    """An attribute on a pool row that appears in no class of the declaration.

    Raised rather than logged: an unclassified attribute means the rollover does
    not know whether to carry it, no reconciler check covers it, and the size
    accounting does not count it. All three are silent failures, which is why the
    only useful response is to stop.
    """


def assert_row_fully_classified(item: dict[str, Any]) -> None:
    """Raise unless every attribute on `item` is classified.

    The closed-world assertion, in the form the four consumers call. `F2` adding an
    attribute without classifying it breaks this at `F2`'s merge -- loudly, and at
    the moment the attribute appears -- instead of having the attribute evaporate on
    the 1st of the following month.
    """
    extra = unclassified_pool_attributes(item)
    if extra:
        raise UndeclaredPoolAttributeError(
            f"{sorted(extra)} appear on the pool row and in no class of "
            f"POOL_ROW_ATTRIBUTES: the rollover does not know whether to carry "
            f"them, no reconciler check covers them, and the size accounting does "
            f"not count them. Classify each one in dynamo/pool_row_schema.py."
        )
