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

WHAT CONSUMERS READ IS A MAPPING, AND IT IS DERIVED. `POOL_ROW_ATTRIBUTES` is
`{spec.name: spec}` built from `_DECLARATIONS`. The mapping is the right shape to
read: every consumer either looks an attribute up by name or walks all of them, and
the closed-world check's central operation is "is this key declared?" -- a mapping's
primitive, and for a sequence an index it has to build before the question can be
asked.

Deriving it rather than writing it out is the part worth explaining. A hand-written
mapping would put each attribute's name in two places, the key and the spec, and two
places holding one string is a redundancy that has to be watched -- a copy-paste that
changed one and not the other would declare a class for an attribute nothing on the
row is called and leave the real one unclassified, which is the declaration's own
failure mode arriving through the declaration. Deriving the mapping removes the
second copy instead of guarding it, so there is nothing left to watch. `name` stays on
the spec, because a spec is read alone in places the key is not there to help -- a
reconciler finding, a debugger, a traceback -- and removing the field to kill the
disagreement would buy that with a spec that cannot say what it describes.

One check survives, and it is a check rather than a watcher: a literal can repeat a
name, and the comprehension would silently keep whichever entry came last. There is
no laxer way to answer that than to ask it.

AN ENTRY ADDED TO EXPLAIN A FUTURE OBLIGATION DISCHARGES IT. This is a set of
obligations, not a set of notes. An attribute that does not exist yet must NOT be
pre-registered with a placeholder writer and an exemption saying its real check is
coming, because the completeness test then passes the moment the attribute appears --
so the change that adds the real writers can forget the real check, and nothing
anywhere says so. That loud failure at the next merge is the entire reason this shape
was chosen over an appendable list, and a helpful placeholder is what quietly trades
it away. Guidance about a future attribute goes HERE, where it informs whoever
classifies it without telling the test that somebody already has.

GUIDANCE FOR THE ATTRIBUTES NOT YET CLASSIFIED. Two arrive with granting, and each
carries a decision that would be expensive to rediscover:

  * `pool_granted_microusd` -- the approved raise, added on top of the baseline.
    Classify it as RESET, and reset it BY OMISSION rather than by writing a zero:
    absence and zero mean the same thing for this attribute, since `ADD` on a missing
    numeric attribute creates it, so the cheaper reading is free. That is exactly what
    is NOT true of `manual_limit_microusd`, where zero is a figure meaning "refuse
    every request" and absence means "follow the seats". Getting the two backwards
    inverts the feature, which is why each has to say which it is where it is
    declared. And resetting it at the period boundary is safe ONLY because a grant's
    `expires_at` is pinned to at most the period end -- without that pin, the
    rollover's reset destroys live granted capacity every 1st, silently, on every
    granted row at once. If that pin is ever loosened, this classification has to
    change with it.
  * the aggregate grant cap -- the ceiling on what any approver may grant. Its
    ABSENCE means "derived from the baseline, evaluated now" rather than a stored
    default, because a materialised default freezes at backfill time: a tenant that
    later hires would keep a cap sized to the baseline it had when the backfill ran,
    quietly wrong in the direction of refusing legitimate approvals. Classify it as
    CARRIED at rollover -- it is the attribute whose evaporation on the 1st would be
    hardest to see, because a missing cap reads as a derived one rather than as an
    error.

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

#: The specs, in declaration order. F2 adds its grant cap here, in the same shape,
#: or the closed-world test fails at F2's merge -- loudly, and at the moment the
#: attribute appears, rather than silently on the 1st of the following month.
#:
#: Consumers read `POOL_ROW_ATTRIBUTES` below, which is DERIVED from this. Writing
#: the mapping out by hand would put each attribute's name in two places -- the key
#: and the spec -- and two places holding one string is a redundancy that has to be
#: watched. Deriving it means there is only ever one string, so there is nothing to
#: watch: the key IS the name, by construction.
_DECLARATIONS: tuple[PoolAttribute, ...] = (
    PoolAttribute(
        name="tenant_id",
        rollover=ROLLOVER_CARRIED,
        writers=("dynamo.tenant_budgets:TenantBudgetsRepository._seed_pool_row",),
        max_value_bytes=128,
        exemption="the partition key; it identifies the row rather than describing it",
    ),
    PoolAttribute(
        name="sk",
        rollover=ROLLOVER_DERIVED,
        writers=("dynamo.tenant_budgets:TenantBudgetsRepository._seed_pool_row",),
        max_value_bytes=32,
        exemption="the sort key; the new period's row has the new period in it by "
                  "construction, so carrying it verbatim would be the bug",
    ),
    PoolAttribute(
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
    PoolAttribute(
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
    PoolAttribute(
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
    PoolAttribute(
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
    PoolAttribute(
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
    PoolAttribute(
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
    PoolAttribute(
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
    PoolAttribute(
        name="pool_reclaimed_microusd",
        rollover=ROLLOVER_RESET,
        reset_by=RESET_BY_ZERO,
        writers=("mvp._pipeline:_reclaim_expired_holds",),
        max_value_bytes=_MAX_POOL_MICROUSD_DIGITS,
        exemption="reconciled against the credit ledger; same owner as reserved",
    ),
    PoolAttribute(
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
    PoolAttribute(
        name="version",
        rollover=ROLLOVER_DERIVED,
        writers=("(every writer in this module stamps it)",),
        max_value_bytes=4,
        exemption="a schema marker; the new row is stamped at the current version",
    ),
    PoolAttribute(
        name="updated_at",
        rollover=ROLLOVER_DERIVED,
        writers=("(every writer in this module stamps it)",),
        max_value_bytes=40,
        exemption="a timestamp; carrying the old row's would misdate the new one",
    ),
    PoolAttribute(
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
)

#: The one declaration, as every consumer reads it: attribute name -> spec.
#:
#: DERIVED from `_DECLARATIONS`, so the key cannot disagree with the spec's own
#: `name`: there is one string, not two, and therefore no check to keep. `name` stays
#: on the spec because a spec is read alone in places the key is not there to help --
#: a reconciler finding, a debugger, a traceback -- and dropping the field to remove
#: the disagreement would pay for it with a spec that is anonymous exactly where it
#: is read by itself.
POOL_ROW_ATTRIBUTES: dict[str, PoolAttribute] = {a.name: a for a in _DECLARATIONS}

# A literal can still repeat a name, and the comprehension above would silently keep
# whichever entry came last -- so `seat_count` could be declared twice, in two
# different rollover classes, and the surviving one would be an accident of order.
# There is no laxer way to answer that question than to ask it, which is what makes
# this a real guard rather than a watcher over a redundancy that should not exist.
if len(POOL_ROW_ATTRIBUTES) != len(_DECLARATIONS):
    _seen: set[str] = set()
    _dupes = sorted({a.name for a in _DECLARATIONS
                     if a.name in _seen or _seen.add(a.name)})
    raise RuntimeError(
        f"_DECLARATIONS declares {_dupes} more than once; one attribute has one "
        f"classification, and the mapping would have silently kept the last")

#: The attributes that MOVE THE CEILING. Names only -- an attribute bears the ceiling
#: whether or not it has been classified yet, which is why `POOL_GRANTED_ATTR` is here
#: while it is deliberately absent from `_DECLARATIONS`. Classifying it later feeds its
#: writers into `ceiling_writers()` with no further edit to this list.
#:
#: The writer list itself is DERIVED from the classification rather than restated. The
#: ceiling-writer document test reads it, so a writer added to `pool_limit_microusd`
#: appears in the document's obligation immediately, and a hardcoded list cannot go
#: green while naming a subset.
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
