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
    # write, in FINAL (post-M4, no `sizing`) form. `seat_rate_microusd`, not
    # `seat_monthly_usd` -- fixed after re-verifying against the
    # implementation post-merge: the mapping conversion this test was
    # waiting for landed, and it exposed that this file's OWN attribute name
    # was stale (a second, independent bug the earlier tuple-shape failure
    # had been masking).
    expected = {
        "tenant_id", "sk",
        "pool_limit_microusd", "pool_headroom_microusd",
        "pool_reserved_microusd", "pool_settled_microusd",
        "seat_count", "manual_limit_microusd", "seat_rate_microusd",
        "status", "version", "updated_at",
    }
    missing = expected - set(POOL_ROW_ATTRIBUTES)
    assert not missing, f"POOL_ROW_ATTRIBUTES is missing: {missing}"


def test_every_entry_is_well_formed():
    """Every entry states a rollover class, at least one writer, and either
    a covering check or an explicit, reasoned exemption -- an entry with
    neither is the same gap this whole mechanism exists to close, one level
    up.

    Field names fixed after re-verifying against the implementation
    post-merge: `PoolAttribute` has `check`/`exemption` (a single
    `Optional[str]` field each, mutually exclusive by construction), not
    `reconciler_check`/`exempt`/`exempt_reason` -- another bug the earlier
    tuple-shape `AttributeError` had been masking."""
    from dynamo.pool_row_schema import POOL_ROW_ATTRIBUTES

    bad = []
    for name, spec in POOL_ROW_ATTRIBUTES.items():
        if spec.rollover not in ("carried", "derived", "reset"):
            bad.append(f"{name}: rollover={spec.rollover!r} is not carried/derived/reset")
        if not spec.writers:
            bad.append(f"{name}: no writers declared")
        if bool(spec.check) == bool(spec.exemption):
            bad.append(f"{name}: exactly one of check/exemption must be set")
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


def test_pool_granted_is_registered_with_real_content_not_a_placeholder():
    """Re-adjudicated on the integration branch, where F2 has already merged.

    The original form of this test asserted bare absence:
    `"pool_granted_microusd" not in POOL_ROW_ATTRIBUTES`, on the reasoning
    that classifying F2's attribute is "F2's own merge-time obligation" and
    F1 must not do it on F2's behalf. That is true of an F1-only branch,
    where the real writers (`grant_apply_txn_item`, `grant_revoke_txn_item`)
    and the real reconciler check (`pool_granted_matches_active_grants`,
    registered in `mvp/grants.py`) do not exist yet -- there, an entry would
    necessarily be a placeholder standing in for work not yet done, which is
    exactly the anti-pattern B1's own module docstring warns against
    ("AN ENTRY ADDED TO EXPLAIN A FUTURE OBLIGATION DISCHARGES IT").

    On THIS branch, F2's writers and check already exist and are registered
    (verified below), so an absent entry would itself be the silent gap B1
    exists to catch, not the property this test is supposed to police. The
    teeth this test guards are therefore narrower than bare absence: the
    entry must never be a placeholder -- a bracketed non-site standing in for
    `writers`, or an `exemption` standing in for a check that was never
    written. `test_assert_row_fully_classified_catches_an_unclassified_attribute`
    above already proves the generic closed-world mechanism catches a
    wholly-missing entry; this test proves the specific entry that exists is
    the real thing, not a discharged obligation."""
    from dynamo.pool_row_schema import POOL_ROW_ATTRIBUTES
    from mvp.observability.quota_reconciler import missing_declared_checks

    spec = POOL_ROW_ATTRIBUTES.get("pool_granted_microusd")
    assert spec is not None, (
        "pool_granted_microusd is unclassified even though F2's grant apply/"
        "revoke writers and its reconciler check already exist in this "
        "integration branch -- that is the silent gap B1 exists to catch, "
        "not a pass condition"
    )
    assert spec.writers and not any(w.startswith("(") for w in spec.writers), (
        f"pool_granted_microusd is classified with a placeholder writer "
        f"{spec.writers!r} instead of the real grant apply/revoke sites -- "
        f"this is the exact anti-pattern B1's docstring warns against"
    )
    assert spec.check and not spec.exemption, (
        "pool_granted_microusd must carry a real reconciler check, not an "
        "exemption standing in for one that was never written"
    )
    assert spec.check not in missing_declared_checks(), (
        f"pool_granted_microusd names check {spec.check!r} but nothing "
        f"registered a check under that name -- the check field is quoting "
        f"a name rather than pointing at real code"
    )
