"""B1 (Seam amendment to the F1 contract): one closed-world declaration of
the pool row's attributes, replacing four separate lists (the rollover's
carried set, R14a's writer list, the reconciler's checks, F4's measured
attribute set) that each required F2 to remember to append to.

The decision, verbatim from the integration owner's seam notes (S1): "An appendable list can still be
forgotten silently... F1 declares every attribute on the pool row, and every
attribute must land in a class... A test fails on any attribute present on a
row that is in no class."

This file tests the mechanism's teeth directly: that a row carrying an
attribute absent from the declaration is CAUGHT, not silently ignored. That
is the property that turns "F2 forgot to classify `pool_granted`" into a
loud failure at F2's merge instead of a cap that evaporates on the 1st.

Today `dynamo.pool_row_schema` does not exist at all -- no `AttributeSpec`,
no `POOL_ROW_ATTRIBUTES`, no `assert_row_fully_classified` -- every test below
fails on `ModuleNotFoundError`.

Correction (integration review, after this file's first draft): the
declaration lives in `backend/dynamo/pool_row_schema.py`, a dedicated leaf
module, NOT in `dynamo.tenant_budgets`. Four separate consumers (the
rollover, the reconciler under `mvp/observability/`, a documentation test,
and the size guard in `iac`/`bench`) import it, and a module that many areas
depend on should not live inside the largest of them; every other `dynamo/`
module is also named for an entity and exports a repository, which a schema
declaration is not. There is deliberately NO re-export from `tenant_budgets`
-- an alias would recreate exactly the two-schema-authority risk this
declaration exists to close (the byte-width guard in `iac`/`bench` already
imports `from dynamo import pool_row_schema`; a second copy in
`tenant_budgets` would let that guard measure a shape the rollover and
reconciler do not actually use). Every import below is
`from dynamo.pool_row_schema import ...`, never `dynamo.tenant_budgets`.

Design note: /Users/akazawt/tmp/stratoneed/change-pipeline/quota-raise-and-archive/design-F1.md
section 0a.
"""
from __future__ import annotations


def test_the_declaration_exists_and_covers_every_attribute_f1_itself_writes():
    from dynamo.pool_row_schema import POOL_ROW_ATTRIBUTES

    # The row shape this file (and R1/R2/R8/R16/R20's own tests) actually
    # write, in FINAL (post-M4, no `sizing`) form.
    expected = {
        "tenant_id", "sk",
        "pool_limit_microusd", "pool_headroom_microusd",
        "pool_reserved_microusd", "pool_settled_microusd",
        "seat_count", "manual_limit_microusd", "seat_monthly_usd",
        "status", "version", "updated_at",
    }
    missing = expected - set(POOL_ROW_ATTRIBUTES)
    assert not missing, f"POOL_ROW_ATTRIBUTES is missing: {missing}"


def test_every_entry_is_well_formed():
    """Every entry states a rollover class, at least one writer, and either
    a reconciler check or an explicit, reasoned exemption -- an entry with
    neither is the same gap this whole mechanism exists to close, one level
    up."""
    from dynamo.pool_row_schema import POOL_ROW_ATTRIBUTES

    bad = []
    for name, spec in POOL_ROW_ATTRIBUTES.items():
        if spec.rollover not in ("carried", "reset"):
            bad.append(f"{name}: rollover={spec.rollover!r} is not carried/reset")
        if not spec.writers:
            bad.append(f"{name}: no writers declared")
        if spec.reconciler_check is None and not spec.exempt:
            bad.append(f"{name}: no reconciler_check and not exempt")
        if spec.exempt and not spec.exempt_reason:
            bad.append(f"{name}: exempt with no reason given")
    assert not bad, "malformed POOL_ROW_ATTRIBUTES entries:\n" + "\n".join(bad)


def test_assert_row_fully_classified_passes_a_row_built_from_declared_keys_only():
    from dynamo.pool_row_schema import POOL_ROW_ATTRIBUTES, assert_row_fully_classified

    row = {name: "x" for name in POOL_ROW_ATTRIBUTES}
    assert_row_fully_classified(row)  # must not raise


def test_assert_row_fully_classified_catches_an_unclassified_attribute():
    """The teeth: a row carrying `pool_granted` -- F2's attribute, added
    without an entry here -- must be caught. This is the exact failure this
    mechanism exists to convert from silent to loud."""
    from dynamo.pool_row_schema import assert_row_fully_classified

    row = {
        "tenant_id": "acme-eng", "sk": "BUDGET#2026-09",
        "pool_limit_microusd": 100, "pool_headroom_microusd": 100,
        "pool_reserved_microusd": 0, "pool_settled_microusd": 0,
        "seat_count": 1,
        "pool_granted": 50,  # <-- unclassified: F2 forgot to register this
    }
    raised = False
    try:
        assert_row_fully_classified(row)
    except Exception as e:
        raised = True
        assert "pool_granted" in str(e), (
            f"the raised error does not name the unclassified attribute: {e}"
        )
    assert raised, (
        "assert_row_fully_classified did not raise on a row carrying an "
        "attribute (pool_granted) absent from POOL_ROW_ATTRIBUTES -- the "
        "closed-world check has no teeth"
    )


def test_pool_granted_itself_is_not_pre_registered_by_f1():
    """B1's declaration is closed-world over what F1 writes; it is not F1's
    job to pre-classify F2's own attribute on F2's behalf (that registration,
    and the merge-time failure if it is skipped, belongs to F2's own PR)."""
    from dynamo.pool_row_schema import POOL_ROW_ATTRIBUTES

    assert "pool_granted" not in POOL_ROW_ATTRIBUTES, (
        "F1 must not pre-register pool_granted -- classifying it is F2's "
        "own merge-time obligation, which is the whole point of B1's design"
    )
