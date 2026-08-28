"""
Formal (SMT) verification of "which price, when": snapshot pinning and the
sentinel discipline.

WHY THIS FILE EXISTS
--------------------
`test_rating_formal_z3.py` proves the ceiling is sound provided the rate is
pinned, and its sanity test shows that letting settle re-read a risen price
breaks the ceiling even when the token counts behave. That makes pinning a
structural member of the ceiling rather than an audit nicety.

Read the scope carefully, because an earlier draft of these two files pointed at
each other and left the obligation nowhere: file 1 said the pinning protocol was
proved here, and this file said it was deferred to a differential test. What is
established here is that pinning is SUFFICIENT for the ceiling and that violating
it is enough to break the ceiling. That the settle code actually honours the
pinned rate is still undischarged, and it is recorded that way in
`docs/EVIDENCE.md` rather than implied by a cross-reference.

The second half is the sentinel discipline. A version label on a terminal event
is a claim about where the amount came from. If the code can stamp a real version
on an amount that did NOT come from that version's frozen rates, then a
fabricated or accidental rate passes the provenance check as well as the
arithmetic one, and the only independent handle on the boundary with the
administrator is gone. So the property is a BICONDITIONAL, not an implication in
the convenient direction.

WHAT SMT CANNOT DO HERE
-----------------------
Whether the Python at settle actually re-reads `CURRENT` is a property of the
code, not of any encoding, and no amount of Z3 establishes it. What is proved
below is the CONSEQUENCE: charging at a rate no greater than the pinned one
preserves the ceiling, and charging above it breaks the ceiling even when the
token counts behave. The obligation that the code honours the pinned rate is
discharged by a differential test that flips the pointer between reserve and
settle — not here. Until that test exists, these proofs establish why pinning
matters, not that it holds.

METHOD
------
As in the sibling files: encode, assert the NEGATION is UNSAT, and pair each
proof with a `sat` sanity test that deletes the guard and confirms Z3 finds the
bug, so the harness cannot be vacuous.

ASSUMPTIONS
-----------
 C1. A pricing version's rate rows are immutable once written. This is a
     discipline inside `set_rates`, NOT a condition expression and NOT an IAM
     boundary, so it is an axiom here and is recorded as undischarged. An admin
     `UpdateItem` that rewrites a row invalidates every provenance conclusion,
     which is precisely why the corresponding evidence row says "as the rows
     exist at verification time".
 C2. The scope is terminals written under the current schema. Legitimate
     historical terminals predate snapshotting and carry the unversioned
     sentinel; an unscoped biconditional would be false about them and the
     proof would be about a system nobody runs.
 C3. Reads of a single item and writes of a single item serialise, as in A2 of
     `test_billing_formal_z3.py` — AWS's documented semantics, taken as an axiom.
"""

import pytest
import z3

Z3_TIMEOUT_MS = 60_000

z3.set_param("smt.random_seed", 0)
z3.set_param("sat.random_seed", 0)

# How the amount was priced. Four causes, matching the four sentinel constants the
# code actually ships — an earlier draft collapsed the legacy and external-amount
# causes into one and then "proved" that every cause gets its own label, which is
# a proof about a model that had already discarded the distinction.
SNAPSHOT_OK = 0        # reserve froze rates and settle used them
SNAPSHOT_FAILED = 1    # reserve tried to freeze and the table read failed
LEGACY_NO_VERSION = 2  # a reservation that predates snapshotting
EXTERNAL_AMOUNT = 3    # a client-declared fixed amount, derived from no rate
CAUSES = (SNAPSHOT_OK, SNAPSHOT_FAILED, LEGACY_NO_VERSION, EXTERNAL_AMOUNT)

# What ends up in `pricing_version`. Sentinels are distinct per cause on purpose,
# so an alarm can tell a degraded path from a legacy one.
STAMP_REAL_VERSION = 0
STAMP_SNAPSHOT_FAILED_SENTINEL = 1
STAMP_UNVERSIONED_SENTINEL = 2
STAMP_EXTERNAL_AMOUNT_SENTINEL = 3


def _solver() -> z3.Solver:
    s = z3.Solver()
    s.set("timeout", Z3_TIMEOUT_MS)
    return s


def _assert_unsat(s: z3.Solver, what: str) -> None:
    result = s.check()
    assert result == z3.unsat, f"{what}: expected unsat, got {result}"


def _assert_sat(s: z3.Solver, what: str) -> None:
    result = s.check()
    assert result == z3.sat, f"{what}: expected sat (bug reachable), got {result}"


# ---------------------------------------------------------------------------
# G2 — settle charges at the version reserve pinned, whatever CURRENT does
# ---------------------------------------------------------------------------

def test_g2_a_settle_rate_at_or_below_the_pinned_rate_preserves_the_ceiling():
    """Pinning is SUFFICIENT for the ceiling, and this states why.

    For any settle rule charging at some rate `r_settle`, token dominance plus
    `r_settle <= r_pinned` is enough: no dominated usage can settle above what
    was reserved. Pinning is the special case `r_settle == r_pinned`, so it
    inherits the guarantee without having to argue that prices only fall.

    Stated over the reals, where nonlinear arithmetic is decidable and the
    integers are a subset — the same move `test_rating_formal_z3.py` documents
    for its lemma B. An earlier draft of this test asserted that two
    syntactically identical expressions were equal, which is a tautology and
    proved nothing about anything; this is the replacement.
    """
    s = _solver()
    tok_reserved, tok_actual = z3.Real("tok_reserved"), z3.Real("tok_actual")
    rate_pinned, rate_settle = z3.Real("rate_pinned"), z3.Real("rate_settle")
    s.add(tok_reserved >= 0, tok_actual >= 0, tok_actual <= tok_reserved)
    s.add(rate_pinned >= 0, rate_settle >= 0, rate_settle <= rate_pinned)
    s.add(z3.Not(tok_actual * rate_settle <= tok_reserved * rate_pinned))
    _assert_unsat(s, "G2 settle at or below the pinned rate preserves the ceiling")


def test_g2_sanity_a_settle_rate_above_the_pinned_rate_breaks_it():
    """SANITY: let the settle rate exceed the pinned one — exactly what
    re-reading `CURRENT` after a price rise does — and a dominated request
    settles above its reservation.

    Together with the test above, this is the entire content of pinning: the
    ceiling survives `r_settle <= r_pinned` and fails otherwise, so the
    obligation is "do not re-read", not "hope prices fall".
    """
    s = _solver()
    tok_reserved, tok_actual = z3.Real("tok_reserved"), z3.Real("tok_actual")
    rate_pinned, rate_settle = z3.Real("rate_pinned"), z3.Real("rate_settle")
    s.add(tok_reserved > 0, tok_actual > 0, tok_actual <= tok_reserved)
    s.add(rate_pinned > 0, rate_settle > rate_pinned)
    s.add(tok_actual * rate_settle > tok_reserved * rate_pinned)
    _assert_sat(s, "G2 sanity: a settle rate above the pinned rate breaks it")


def test_g2_the_write_order_makes_a_dangling_read_impossible_in_time():
    """The ORDERING argument behind "a version read after its rows cannot dangle",
    and nothing more than that.

    Both reviewers pointed out that the earlier name and docstring promised a
    storage result while the encoding contains only a time inequality. So the
    claim is now scoped to what is actually proved: if rows are written before the
    pointer flips, and a reader reads the pointer no earlier than the flip and the
    rows no earlier than the pointer, then the rows were written before they were
    read. Read consistency mode, partial failure, replica visibility and item-level
    durability are NOT modelled here and are not implied.
    """
    s = _solver()
    rows_written_at = z3.Int("rows_written_at")
    pointer_flipped_at = z3.Int("pointer_flipped_at")
    read_pointer_at = z3.Int("read_pointer_at")
    read_rows_at = z3.Int("read_rows_at")

    s.add(rows_written_at < pointer_flipped_at)          # the write-order guard
    s.add(read_pointer_at >= pointer_flipped_at)         # the reader saw the flip
    s.add(read_rows_at >= read_pointer_at)               # rows read after pointer
    s.add(z3.Not(read_rows_at > rows_written_at))        # negation: rows unseen
    _assert_unsat(s, "G2 a version read after its rows cannot dangle")


def test_g2_sanity_flipping_before_writing_dangles():
    """SANITY: reverse the write order and the same reader can read rows that were
    not yet written.

    The reader's full sequence is kept — pointer first, then rows — so the only
    change from the proof above is the deleted write-order guard. The earlier
    version omitted `read_pointer_at` and so did not model the reader at all.
    """
    s = _solver()
    rows_written_at = z3.Int("rows_written_at")
    pointer_flipped_at = z3.Int("pointer_flipped_at")
    read_pointer_at = z3.Int("read_pointer_at")
    read_rows_at = z3.Int("read_rows_at")
    s.add(pointer_flipped_at < rows_written_at)          # the guard, reversed
    s.add(read_pointer_at >= pointer_flipped_at)
    s.add(read_rows_at >= read_pointer_at)
    s.add(read_rows_at < rows_written_at)                # rows still absent
    _assert_sat(s, "G2 sanity: flipping before writing dangles")


# ---------------------------------------------------------------------------
# G4 — the sentinel biconditional
# ---------------------------------------------------------------------------

def _stamp_under_the_rule(priced_by: z3.ArithRef) -> z3.ArithRef:
    """The stamping rule under proof: a real version only when a frozen snapshot
    produced the amount, and a DISTINCT sentinel per other cause.
    """
    return z3.If(
        priced_by == SNAPSHOT_OK, STAMP_REAL_VERSION,
        z3.If(priced_by == SNAPSHOT_FAILED, STAMP_SNAPSHOT_FAILED_SENTINEL,
              z3.If(priced_by == LEGACY_NO_VERSION, STAMP_UNVERSIONED_SENTINEL,
                    STAMP_EXTERNAL_AMOUNT_SENTINEL)),
    )


def test_g4_real_version_stamped_if_and_only_if_snapshot_priced_it():
    """The biconditional, in both directions at once.

    The forward direction alone ("a snapshot charge gets a version") is the easy
    half and the useless one. The reverse ("a version label implies a snapshot
    charge") is what a provenance check relies on: without it, an amount priced
    off the live table during a snapshot failure could wear a real version and
    pass verification.
    """
    s = _solver()
    priced_by = z3.Int("priced_by")
    s.add(z3.Or([priced_by == c for c in CAUSES]))
    stamped = _stamp_under_the_rule(priced_by)
    s.add(z3.Not((stamped == STAMP_REAL_VERSION) == (priced_by == SNAPSHOT_OK)))
    _assert_unsat(s, "G4 real version iff snapshot priced it")


def test_g4_sanity_stamping_current_on_snapshot_failure_breaks_it():
    """SANITY: stamp a real version when the snapshot read failed — the plausible
    bug, since a live rate is right there — and the biconditional falls.

    This is the exact shape that would let a self-consistent fabricated rate
    survive both the arithmetic and the provenance check.
    """
    s = _solver()
    priced_by = z3.Int("priced_by")
    s.add(z3.Or([priced_by == c for c in CAUSES]))
    buggy_stamp = z3.If(
        z3.Or(priced_by == SNAPSHOT_OK, priced_by == SNAPSHOT_FAILED),
        STAMP_REAL_VERSION, STAMP_UNVERSIONED_SENTINEL,
    )
    # The cause is left FREE rather than pinned to SNAPSHOT_FAILED. Pinning it made
    # the If fold to a constant and the solver had nothing to search, which both
    # reviewers called out. Now Z3 must find the cause that breaks the
    # biconditional, which is the content of the check.
    s.add(z3.Not((buggy_stamp == STAMP_REAL_VERSION) == (priced_by == SNAPSHOT_OK)))
    _assert_sat(s, "G4 sanity: stamping CURRENT on snapshot failure breaks it")


def test_g4_each_cause_gets_its_own_label():
    """Two different non-snapshot causes never collapse onto the same sentinel.

    A single "no version" label would make a degraded path — the snapshot read
    failing, which must raise an alarm — indistinguishable from a legacy
    reservation that never had a version to begin with.
    """
    s = _solver()
    a, b = z3.Int("cause_a"), z3.Int("cause_b")
    for v in (a, b):
        s.add(z3.Or([v == c for c in CAUSES]))
    s.add(a != b)
    s.add(_stamp_under_the_rule(a) == _stamp_under_the_rule(b))
    _assert_unsat(s, "G4 each cause gets its own label")


def test_g4_sanity_one_shared_sentinel_collapses_the_causes():
    """SANITY: collapse the two sentinels into one and the causes become
    indistinguishable.
    """
    s = _solver()
    a, b = z3.Int("cause_a"), z3.Int("cause_b")
    collapsed = lambda v: z3.If(v == SNAPSHOT_OK, STAMP_REAL_VERSION,
                                STAMP_UNVERSIONED_SENTINEL)  # noqa: E731
    for v in (a, b):
        s.add(z3.Or([v == c for c in CAUSES]))
    # Causes left free, as above: Z3 finds the pair that collides rather than
    # being handed it.
    s.add(a != b)
    s.add(collapsed(a) == collapsed(b))
    _assert_sat(s, "G4 sanity: one shared sentinel collapses the causes")


# ---------------------------------------------------------------------------
# The sentinel constants the proofs above are about must be the real ones
# ---------------------------------------------------------------------------

def test_the_modelled_sentinels_are_the_shipped_sentinels():
    """The proofs reason about three distinct causes. If the implementation ever
    collapses two sentinel constants, or drops one, the encoding stops describing
    the system and the proofs above become decoration.

    So the encoding's assumption is discharged here rather than trusted: the
    shipped constants exist and are pairwise distinct.
    """
    from mvp.pricing import (
        EXTERNAL_AMOUNT_SENTINEL,
        SNAPSHOT_FAILED_SENTINEL,
        UNVERSIONED_SENTINEL,
    )

    sentinels = [UNVERSIONED_SENTINEL, SNAPSHOT_FAILED_SENTINEL,
                 EXTERNAL_AMOUNT_SENTINEL]
    assert all(isinstance(v, str) and v for v in sentinels)
    assert len(set(sentinels)) == len(sentinels), (
        "two sentinels collapsed onto the same label — G4's "
        "each-cause-its-own-label proof no longer describes the code"
    )
