"""R14a (the F1 contract): `docs/design/limits.md` and `CONTRACTS.md`
C14 state the rule and its reversibility, and C14.1 gains the boundary it
lacks.

R14a's own "Verified by": "The claim lint; a test asserts the document names
every writer of the ceiling." Mirrors the existing
`test_limits_doc_names_reserve_limits_l6.py` pattern: a doc-content test that
fails on the doc simply not saying the thing yet, rather than on any code
import.

Writing the doc content itself is production documentation (docs/design/ is
explicitly out of this test-authoring assignment's remit); this file only
pins what the eventual prose must contain.

Today `docs/design/limits.md` section 4 and `CONTRACTS.md` C14 describe the
OLD `sizing="per_seat"`/`"fixed"` rule this contract replaces -- neither
document mentions `manual_limit`, `seat_count`, or `follow_seats` at all, so
every assertion below fails today on plain string absence.

Seam amendments B2/B4/B5/B8 change this file's shape, not just its content:

  - B5: the writer-list test no longer hardcodes which writers to look for.
    It derives the expected set from `dynamo.pool_row_schema.POOL_ROW_ATTRIBUTES`
    (B1's closed-world declaration) -- "a hardcoded list passes while naming a
    subset once F2 adds apply and revoke; a green test over an incomplete
    document." This makes the test depend on B1 landing first, which is the
    point: a doc test that could pass without the declaration existing would
    be checking a literal, not the declaration.
  - B2: the rule must be stated in FINAL form -- `pool_limit = baseline +
    coalesce(pool_granted, 0)`, "plus any granted amount, zero until grants
    exist" -- not `pool_limit = baseline` with an implicit promise that F2
    edits the sentence later. Writing it provisionally and patching it is a
    *change* the claim ratchet cannot see as a weakening.
  - B4, corrected after review: there is NO false "fixed size, cannot grow"
    claim anywhere in the docs for this file to demand a rewrite of. The
    sentence at `docs/design/pending-protocol.md:103` ("the marker item is
    FIXED-SIZE...") is about the MARKER item (`SK=MARKER#<hold_id>`), a
    separate item this epic never touches -- that sentence stays TRUE and
    must not be edited or asserted-changed. What survives from B4 is two
    real obligations: (a) the `PoolItemSizeBytes` gauge and its
    `PoolItemSizeGrowth` alarm (`iac/lib/ecs-stack.ts`) are CODE calibrated
    to the pre-epic row, and F1 growing the row means that calibration must
    be re-derived from B1's declaration rather than left as an
    unexplained literal -- this is the load-bearing half, tested in
    `iac/test/pool-item-size-recalibration.test.ts`; (b)
    `pending-protocol.md:105` ("the single-partition WCU ceiling remains
    bounded by the pool item itself") stays true as a claim but names no
    magnitude -- it earns one sentence stating the new bound, tested below.
  - B8: `limits.md` states that no migration phase may be re-run after F2
    merges -- M3's fail-stale read would otherwise fold a future
    `pool_granted` permanently into the manual figure on every row at once.
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
LIMITS_DOC = ROOT / "docs" / "design" / "limits.md"
CONTRACTS_DOC = ROOT / "docs" / "design" / "CONTRACTS.md"


def _limits_text() -> str:
    assert LIMITS_DOC.is_file(), f"{LIMITS_DOC} does not exist"
    return LIMITS_DOC.read_text()


def _contracts_text() -> str:
    assert CONTRACTS_DOC.is_file(), f"{CONTRACTS_DOC} does not exist"
    return CONTRACTS_DOC.read_text()


def test_limits_doc_names_the_two_new_attributes():
    text = _limits_text()
    assert "manual_limit" in text, (
        "docs/design/limits.md does not mention manual_limit_microusd -- it "
        "still describes the sizing='fixed'/'per_seat' rule this contract replaces"
    )
    assert "seat_count" in text, "docs/design/limits.md does not mention seat_count"


def test_limits_doc_states_the_zero_is_not_absence_sentinel():
    """The one sentence R1 calls out as the load-bearing distinction: zero is
    a figure, absence is the sentinel. Checked as co-occurrence within one
    paragraph-sized window (not "the word absent appears somewhere in the
    document" and "the word zero appears somewhere in the document"
    independently -- the OLD sizing-era prose already contains both words in
    unrelated sentences, e.g. "zero is the honest ceiling for a tenant nobody
    is a member of yet", which would make an independent check pass today
    for the wrong reason)."""
    text = _limits_text().lower()
    idx = text.find("manual_limit")
    assert idx != -1, "docs/design/limits.md does not mention manual_limit at all"
    window = text[max(0, idx - 400): idx + 400]
    assert "absen" in window and "zero" in window, (
        "docs/design/limits.md mentions manual_limit, but not within a "
        "paragraph that also contrasts it against absence and zero -- the "
        "sentinel-is-absence-not-falsiness sentence R1 calls out is missing"
    )


def test_limits_doc_states_reversibility_via_follow_seats():
    text = _limits_text()
    assert "follow_seats" in text, (
        "docs/design/limits.md does not document the {\"follow_seats\": true} "
        "reversal path -- the old sizing='fixed' rule was one-way except "
        "through a second explicit set, and this contract's reversibility "
        "claim needs its own sentence"
    )


def test_limits_doc_names_every_writer_the_declaration_lists():
    """B5: the expected writer set is DERIVED from
    `dynamo.pool_row_schema.POOL_ROW_ATTRIBUTES` (B1's closed-world
    declaration -- a dedicated leaf module, not `dynamo.tenant_budgets`;
    an integration review found the union had grown two schema
    authorities and there is deliberately no re-export), never hardcoded
    here -- a hardcoded list would keep passing the moment F2 adds
    `apply`/`expiry_revoke`/`early_revoke`/`repair` writers for
    `pool_granted` without anyone updating this file, which is exactly the
    "green test over an incomplete document" B5 exists to prevent.

    Narrowed from a raw union over every declared attribute to
    `dynamo.pool_row_schema.ceiling_writers()` after checking the document
    itself: `limits.md`'s own "Every writer of this ceiling" section names
    its derivation source explicitly --
    "`dynamo.pool_row_schema.ceiling_writers()`, from `POOL_ROW_ATTRIBUTES`
    in that module" -- not a raw union over every attribute on the row. A
    raw union pulls in `pool_reclaimed_microusd`'s writer
    (`mvp._pipeline:_reclaim_expired_holds`), which moves spend bookkeeping,
    not the ceiling; `ceiling_writers()` is the function B1 shipped
    specifically to exclude exactly that (it filters to `CEILING_ATTRS`),
    and it is the one the document already cites. Checking against a
    different, broader set than the one the document says it is deriving
    from would make this test wrong about what "the ceiling" means, not the
    document.

    Matched against the writer's `Class.method` (or bare function) form,
    stripping the `module.path:` prefix -- fixed after checking what the
    document actually cites: `limits.md` already names four of these
    writers as `` `TenantBudgetsRepository.set_manual_limit` ``, never as
    the fully qualified `dynamo.tenant_budgets:TenantBudgetsRepository.
    set_manual_limit` the declaration uses internally for programmatic
    lookup. The qualified form with its module path and colon is this
    module's own bookkeeping key, not a citation convention any prose would
    use, so demanding it appear verbatim would fail the document over a
    formatting choice the declaration never asked it to match.

    Sentinel/placeholder writer markers are not real function names and are
    excluded -- fixed after re-verifying against the implementation
    post-merge: the real placeholders are parenthetical strings like
    `"(F2: the grant apply and revoke writers)"` and
    `"(every writer in this module stamps it)"`, not the `__key__`/
    `__all_writers__` sentinels this test invented before the real module
    existed to check against. `ceiling_writers()` already filters these out
    (`not w.startswith("(")`), so this test's notion of "a real writer"
    matches the declaration's own."""
    from dynamo.pool_row_schema import ceiling_writers

    writers = ceiling_writers()
    assert writers, "ceiling_writers() reports no real writers at all"

    text = _limits_text()
    missing = sorted(
        w for w in writers if w.rsplit(":", 1)[-1] not in text
    )
    assert not missing, (
        f"docs/design/limits.md does not name these writers from "
        f"ceiling_writers(): {missing}"
    )


def test_limits_doc_states_the_rule_in_final_coalesced_form():
    """B2: the rule ships as `pool_limit = baseline + coalesce(pool_granted,
    0)` from day one, not `pool_limit = baseline` with the `+ pool_granted`
    term added later by F2 -- a later edit to this sentence is a *change*
    the claim ratchet cannot see as a weakening."""
    text = _limits_text().lower()
    assert "pool_granted" in text, (
        "docs/design/limits.md does not mention pool_granted at all -- the "
        "rule must be stated in FINAL form (baseline + coalesce(pool_granted, "
        "0)) even though F1 writes no grants"
    )
    assert "coalesce" in text or "zero until" in text or "absence" in text, (
        "docs/design/limits.md mentions pool_granted but not the "
        "coalesce-to-zero framing ('plus any granted amount, zero until "
        "grants exist') that keeps the identity true before F2 ships any"
    )


def test_contracts_c14_states_the_rule_in_final_coalesced_form():
    """Same requirement (B2), on the CONTRACTS.md C14 side."""
    text = _contracts_text().lower()
    assert "pool_granted" in text, (
        "CONTRACTS.md C14 does not mention pool_granted -- the identity must "
        "ship in final coalesced form, not as baseline-only text F2 edits later"
    )


def test_limits_doc_states_the_migration_is_one_shot():
    """B8: 'No migration phase may be re-run after F2 merges' -- a re-run of
    M3's fail-stale read against a row that by then carries pool_granted
    would fold granted money permanently into the manual figure, on every
    row at once."""
    text = _limits_text().lower()
    assert "one-shot" in text or "may not be re-run" in text or "must not be re-run" in text, (
        "docs/design/limits.md does not state that no migration phase may "
        "be re-run after F2 merges (Amendment B8)"
    )


def test_pending_protocol_wcu_bound_sentence_names_the_new_magnitude():
    """B4, corrected: no sentence claims the POOL item is a fixed size that
    cannot grow -- the FIXED-SIZE sentence at `pending-protocol.md:103` is
    about the separate MARKER item (`SK=MARKER#<hold_id>`) and stays true
    unedited; asserting it must change would push an implementer to falsify
    a correct statement, which this file must not do.

    The real, narrower obligation is at `pending-protocol.md:105`: "the
    single-partition WCU ceiling remains bounded by the pool item itself...
    ". That claim stays true as F1 grows the row -- the bound simply moves
    -- but today it names no magnitude at all, so a reader cannot tell
    whether the bound just shifted. One sentence naming the new bound (a
    byte figure, or an explicit reference to the post-F1 attribute set)
    closes that."""
    doc = ROOT / "docs" / "design" / "pending-protocol.md"
    assert doc.is_file(), f"{doc} does not exist"
    text = doc.read_text()
    idx = text.find("single-partition WCU ceiling")
    assert idx != -1, (
        "docs/design/pending-protocol.md no longer contains the "
        "'single-partition WCU ceiling ... bounded by the pool item itself' "
        "sentence at all -- nothing to check against"
    )
    # A narrow window (120 chars, this sentence only) checked against the
    # THREE new attribute names specifically -- not a generic byte/number
    # token. The surrounding paragraph already contains unrelated figures
    # ("~2x", "~2 WCU/item", about the sharded-pool alternative), which made
    # a generic "does a number appear nearby" check pass today for the WRONG
    # reason (confirmed by hand before locking this window down). The
    # sentence must name the actual new attributes, not merely sit near any
    # digit.
    window = text[idx: idx + 120].lower()
    # `seat_monthly_usd` fixed to `seat_rate_microusd`: the former is
    # `dynamo.tenant_budgets.seat_monthly_usd()`, the LIVE function that reads
    # the deployment's configured rate; the row attribute F1 actually stores
    # (`dynamo.pool_row_schema.SEAT_RATE_ATTR`) is `seat_rate_microusd`. The
    # function computes a number; the attribute is what widens the row, which
    # is what this sentence is about -- checking for the function's name here
    # would never find it in a row-width sentence for the right reason.
    names_the_new_attributes = any(
        token in window for token in ("seat_count", "manual_limit_microusd", "seat_rate_microusd")
    )
    assert names_the_new_attributes, (
        "the WCU-ceiling sentence in docs/design/pending-protocol.md still "
        "names no post-F1 magnitude or attribute -- it must state the new "
        "bound (Amendment B4, narrowed) rather than leave a reader unable "
        "to tell the bound moved"
    )


def test_pending_protocol_marker_fixed_size_sentence_is_left_untouched():
    """The sentence this file must NOT demand a change to, checked so a
    future edit that accidentally weakens or removes it is caught: the
    marker item's fixed-size property is unrelated to this epic (F1 never
    touches MARKER# items) and stays true regardless of what happens to the
    pool row."""
    doc = ROOT / "docs" / "design" / "pending-protocol.md"
    assert doc.is_file(), f"{doc} does not exist"
    text = doc.read_text().lower()
    assert "the marker item is fixed-size" in text, (
        "docs/design/pending-protocol.md no longer states that the marker "
        "item is fixed-size -- this sentence is about MARKER# items, which "
        "F1 never touches, and must not have been edited by this epic"
    )


def test_contracts_c14_states_the_new_rule():
    text = _contracts_text()
    assert "manual_limit" in text, (
        "CONTRACTS.md C14 still describes sizing='per_seat'/'fixed' and does "
        "not state the manual_limit/seat_count rule this contract replaces it with"
    )


def test_contracts_c14_1_states_a_seat_cap_or_zero_manual_limit_boundary():
    """C14.1 'gains the boundary it lacks' (R14a). This test does not pick
    which of the two readings the design note's section 8 names is correct -- it
    only requires that C14.1's row state SOME additional boundary beyond the
    bootstrap seed_default one it already has, by requiring either reading's
    key term to appear near C14.1."""
    text = _contracts_text()
    c14_1_start = text.find("**C14.1**")
    assert c14_1_start != -1, "CONTRACTS.md has no C14.1 row"
    # The next clause row (C14.2) bounds how far C14.1's own row extends.
    c14_2_start = text.find("**C14.2**")
    c14_1_row = text[c14_1_start:c14_2_start if c14_2_start != -1 else c14_1_start + 2000]
    reading_a = "MAX_POOL_BUDGET_USD_CENTS" in c14_1_row and "seat_count" in c14_1_row
    reading_b = "manual_limit_microusd" in c14_1_row and "0" in c14_1_row
    assert reading_a or reading_b, (
        "C14.1's row states no boundary beyond the pre-existing bootstrap "
        "seed_default one -- see the design note's section 8 for the two readings "
        "of what boundary it is missing"
    )
