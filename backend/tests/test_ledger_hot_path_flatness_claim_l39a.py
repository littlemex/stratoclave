"""F4 / R39a — the pool item's flatness claim must be DERIVED, not asserted.

WHAT DEFECT THIS CLOSES

`docs/design/ledger-hot-path.md`, `backend/dynamo/tenant_budgets.py`'s
`_estimate_item_size_bytes` docstring, `backend/mvp/_pipeline.py`'s
`pool_item_size` gauge comment, and `iac/lib/ecs-stack.ts`'s alarm comment all
repeat a version of "a fixed-size item cannot grow" as the reason the
WCU-proportional-to-size argument holds and the published
`docs/benchmarks/ledger-latency.md` figures stay comparable. The quota-raise
epic deletes `sizing` and adds THREE attributes to that same item — not two:
`seat_count`, `manual_limit`, `pool_granted`, PLUS the stored seat rate
(amendment A5, seeded at M1 and carried forward by R16) — so "cannot grow"
becomes literally false the moment F1 lands, even though the CONCLUSION it
protects (the item stays inside DynamoDB's first 1,024-byte write unit) still
holds. (B2, `CONTRACT-F4-claims (F4's contract document)`'s "Seam amendments": an earlier version of
this file's docstring named only two attributes, missing the rate — the same
class of over-precision this part already corrected once for the byte count.)

SEAM CORRECTION B1 — WHY THIS FILE ASSERTS A DERIVATION, NOT A NUMBER

An earlier version of this test measured a fixture row seeded with
`sizing="per_seat"` and none of the epic's attributes (see git history at this
file's old line 124) and checked the document's stated figure against THAT
pre-epic measurement. `CONTRACT-F4-claims (F4's contract document)`'s "Seam amendments" section (B1)
names this exactly: the fixture "carries none of the attributes the epic
adds, so the one-write-unit conclusion is drawn from an item that will not
exist when the epic lands" — and F4 could not have done otherwise, since this
worktree branches from `origin/main`, where the new row has no definition.

The fix is structural, not a bigger fixture: F1 now ships a closed-world
declaration of the pool row (S1 in `SEAMS (the integration owner's seam-review document)`) that names every attribute and
its maximum value width, and this test asserts against a WORST-CASE SIZE
COMPUTED FROM THAT DECLARATION, not a number anyone typed in. Three things
derive from the same declaration (F1's, not F4's): the gauge baseline and its
alarm threshold (`iac/test/ecs-stack-pool-item-size-baseline-l39b.test.ts`),
the computed worst-case size asserted under 1024 bytes with the margin
printed (this file), and the figure the document states. So this test's guard
rides F1's (and F2's, once `pool_granted` lands) schema changes instead of
failing twice on the way to the real schema, and it protects the CONCLUSION —
one write unit — rather than rubber-stamping a byte count.

SEAM CORRECTION B3 — WHO OWNS THE FIX

The false "cannot grow" wording is corrected in **F1**, the part that makes it
false (S3 in `SEAMS (the integration owner's seam-review document)`) — not in F4, which merges last and would otherwise
leave the document false, and the size alarm miscalibrated, for the whole
interval between F1's merge and F4's, while the claim lint stays green because
nothing edited the anchored text. F4 keeps the ANCHOR (this test still exists
and still fails until the wording is gone) but not first-owner responsibility
for writing the replacement text — that design work moved to F1's contract.

WHY THIS FAILS TODAY

`docs/design/ledger-hot-path.md` names no measured byte size at all, and no
`backend.dynamo.pool_row_schema` (F1's expected closed-world declaration
module — the name the F4 design note section 1 proposes, to be confirmed or renamed
by F1's own contract) exists in this worktree yet. Both are correct failures:
this worktree is pre-F1.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "design" / "ledger-hot-path.md"

#: A byte figure followed by "byte" or "B" within a few words, so "1,024-byte" and
#: "296 bytes" both match but a stray number elsewhere in the document does not.
BYTE_FIGURE = re.compile(r"\b(\d[\d,]*)\s*(?:bytes?|B\b)")

#: The write-unit boundary. DynamoDB's is 1,024 (1 KiB); accept either spelling.
BOUNDARY_MENTION = re.compile(r"1[,.]?024|1\s*Ki?B\b", re.IGNORECASE)

#: The honest conclusion this sentence must reach: bigger, but still one unit.
CONCLUSION = re.compile(
    r"(fixed at a larger size|still one write unit|one write unit"
    r"|inside .{0,20}first write unit)",
    re.IGNORECASE,
)

#: The false claim this sentence must NOT still make, unqualified, anywhere in the
#: document once F1's replacement lands (B3: F1 writes the fix; F4 anchors it). A
#: literal grep, not a semantic check — deliberately narrow so a legitimate use of
#: "cannot grow" about something else (there is none today) would not false-positive.
FALSE_CLAIM = re.compile(r"fixed[- ]size item cannot grow", re.IGNORECASE)

#: A margin figure — "656 bytes of margin", "margin: 656 bytes" — so the document
#: states the safety margin under the 1024 B boundary explicitly, not just the
#: worst-case figure on its own.
MARGIN_MENTION = re.compile(r"margin[^.]{0,40}?(\d[\d,]*)\s*bytes?|(\d[\d,]*)\s*bytes?"
                             r"[^.]{0,20}?margin", re.IGNORECASE)


def _doc_text() -> str:
    return DOC.read_text()


def test_the_document_names_a_measured_byte_size_and_the_1kib_boundary():
    """R39a's 'Verified by': the document names the measured size, the 1 KiB
    boundary, and the conclusion — not just an unquantified "fixed size" claim."""
    text = _doc_text()
    assert BYTE_FIGURE.search(text), (
        f"{DOC} names no measured byte figure for the pool item — the flatness "
        f"claim must state the size it measured, not just assert fixed-size-ness."
    )
    assert BOUNDARY_MENTION.search(text), (
        f"{DOC} does not name the 1,024-byte (1 KiB) write-unit boundary the "
        f"one-write-unit conclusion depends on."
    )
    assert CONCLUSION.search(text), (
        f"{DOC} does not state the honest conclusion ('fixed at a larger size, "
        f"still one write unit')."
    )


def test_the_document_no_longer_claims_a_fixed_size_item_cannot_grow():
    """The specific false claim this part anchors rather than authors (B3). Once
    F1 deletes `sizing` and adds three attributes (`seat_count`, `manual_limit`,
    `pool_granted`, and the stored seat rate — B2), "a fixed-size item cannot
    grow" is untrue of the shipped schema, and this document must not repeat
    it. The REPLACEMENT wording is F1's design work; this test only refuses
    the false sentence's survival, so it is agnostic to exactly what F1 writes
    instead."""
    text = _doc_text()
    assert not FALSE_CLAIM.search(text), (
        f"{DOC} still claims a fixed-size item cannot grow, which the quota-raise "
        f"epic's own attribute additions falsify. The replacement wording is F1's "
        f"responsibility (SEAMS (the integration owner's seam-review document) S3 / CONTRACT-F4-claims (F4's contract document) amendment B3); this "
        f"test anchors that F1 did it, not what F1 should write."
    )


def test_the_documents_worst_case_figure_is_derived_from_f1s_schema_declaration():
    """B1's replacement for the old range-vs-live-gauge check. Rather than
    comparing the document's figure to a measurement of a PRE-EPIC row (which
    this worktree, branching from origin/main, cannot avoid producing on its
    own), this test computes the worst-case pool-item size from F1's
    closed-world schema declaration — the same source the gauge baseline, the
    alarm threshold, and this document are all supposed to derive from (B1) —
    and requires the document's stated figure to EQUAL that computed value,
    to be under 1024 bytes, and to state the margin.

    Fails today for the correct reason: F1's declaration module does not
    exist in this worktree (pre-F1), so there is nothing to derive from and
    nothing for the document to match yet. Once F1 lands
    `backend.dynamo.pool_row_schema` (or whatever name F1's own contract
    gives it — this import path is F4's proposal, not a constraint on F1),
    this test starts deriving instead of guessing."""
    try:
        from dynamo import pool_row_schema  # type: ignore
    except ImportError:
        pytest.fail(
            "backend.dynamo.pool_row_schema (F1's closed-world pool-row "
            "declaration, S1 in SEAMS (the integration owner's seam-review document)) does not exist in this worktree "
            "yet. The gauge baseline, its alarm threshold, and this "
            "document's worst-case figure are all supposed to derive from "
            "it (CONTRACT-F4-claims (F4's contract document) amendment B1) — until it lands, "
            "there is nothing to derive from."
        )

    computed = pool_row_schema.worst_case_pool_item_bytes()
    assert computed < 1024, (
        f"F1's declared schema computes a worst-case pool item of {computed} "
        f"bytes, which is NOT under the 1,024-byte write-unit boundary — the "
        f"one-write-unit conclusion this whole part exists to keep honest "
        f"would be false. This is a finding for F1, not a wording fix for F4."
    )
    margin = 1024 - computed

    text = _doc_text()
    figures = {int(m.group(1).replace(",", "")) for m in BYTE_FIGURE.finditer(text)}
    assert computed in figures, (
        f"F1's schema declaration computes a worst-case pool item of "
        f"{computed} bytes, but docs/design/ledger-hot-path.md does not "
        f"state that number — the document's figure must be DERIVED from "
        f"the declaration (re-run whenever the declaration changes), not "
        f"independently chosen."
    )
    assert MARGIN_MENTION.search(text), (
        f"the document states a worst-case figure but not the margin below "
        f"the 1,024-byte boundary ({margin} bytes at the currently-declared "
        f"schema) — R39a's 'Verified by' requires the boundary to be stated, "
        f"and a margin makes that boundary a checkable number rather than a "
        f"bare 'it fits'."
    )
