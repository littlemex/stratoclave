"""F4 / R39a — the pool item's flatness claim must be DERIVED, not asserted.

WHAT DEFECT THIS CLOSES

`docs/design/ledger-hot-path.md`, `backend/dynamo/tenant_budgets.py`'s
`_estimate_item_size_bytes` docstring, `backend/mvp/_pipeline.py`'s
`pool_item_size` gauge comment, and `iac/lib/ecs-stack.ts`'s alarm comment all
repeat a version of "a fixed-size item cannot grow" as the reason the
WCU-proportional-to-size argument holds and the published
`docs/benchmarks/ledger-latency.md` figures stay comparable. F1 deletes
`sizing` and adds its OWN three attributes to that same item — `seat_count`,
`manual_limit_microusd`, `seat_rate_microusd` (the stored seat rate; carried
across a period boundary) — so "cannot grow" becomes literally false the
moment F1 lands, even though the CONCLUSION it protects (the item stays
inside DynamoDB's first 1,024-byte write unit) still holds. F2 later adds two
more (`pool_granted_microusd`, and an aggregate cap) — deliberately NOT
declared by F1 (see the update below), so F1's merge only ever falsifies the
wording with respect to its OWN three, and F2's merge falsifies it further.

UPDATE (F1 has landed — elsewhere; NOT in this worktree, which stays pre-F1
per §0 below): `backend/dynamo/pool_row_schema.py` is real. `POOL_ROW_ATTRIBUTES`
is a `dict[str, PoolAttribute]` (a dict, not the tuple an earlier draft of
this file proposed — a key cannot disagree with a name), and
`worst_case_pool_item_bytes()` derives its answer from each entry's declared
maximum value width. **F1 deliberately does NOT classify
`pool_granted_microusd` or the aggregate cap** — pre-classifying an attribute
a LATER part owns would let F2's merge add the writers and forget the
completeness check with nothing saying so; leaving it unclassified is what
makes F2's merge fail loudly instead. So the declared worst case today is the
worst case of what is CURRENTLY classified, not of the whole post-epic row —
it grows by exactly the two attributes F2 classifies when F2 lands, and that
growth is the mechanism working, not drift to pin down. This file's guard
therefore asserts against the LIVE FUNCTION result, never a literal number
(528, as of F1's landing, is cited here for context only — it is not asserted
anywhere below, and must not become a hardcoded expectation).

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
declaration of the pool row (S1 in `SEAMS (the integration owner's seam-review document)`) that names every CLASSIFIED attribute and
its maximum value width, and this test asserts against a WORST-CASE SIZE
COMPUTED FROM THAT DECLARATION, not a number anyone typed in. Three things
derive from the same declaration (F1's, not F4's): the gauge baseline and its
alarm threshold (`iac/test/ecs-stack-pool-item-size-baseline-l39b.test.ts`),
the computed worst-case size asserted under 1024 bytes with the margin
printed (this file), and the figure the document states. So this test's guard
rides F1's (and F2's, once F2 classifies `pool_granted_microusd` and the
aggregate cap) schema changes instead of failing twice on the way to the real
schema, and it protects the CONCLUSION — one write unit — rather than
rubber-stamping a byte count. **The subject of this guard is "the declared
row fits in one write unit", not any particular number** — the number moving
between F1's merge and F2's is the declaration doing its job.

SEAM CORRECTION B3 — WHO OWNS THE FIX

The false "cannot grow" wording is corrected in **F1**, the part that makes it
false (S3 in `SEAMS (the integration owner's seam-review document)`) — not in F4, which merges last and would otherwise
leave the document false, and the size alarm miscalibrated, for the whole
interval between F1's merge and F4's, while the claim lint stays green because
nothing edited the anchored text. F4 keeps the ANCHOR (this test still exists
and still fails until the wording is gone) but not first-owner responsibility
for writing the replacement text — that design work moved to F1's contract.

WHY THIS FAILS TODAY

`docs/design/ledger-hot-path.md` names no measured byte size at all, and
`backend.dynamo.pool_row_schema` — confirmed real elsewhere, per the update
above — does not exist in THIS worktree yet, which stays pinned to
`origin/main` (see §0 in the F4 design note). Both are correct failures: this
worktree is pre-F1, and will not itself synthesize F1's code by report alone.
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
    F1 deletes `sizing` and adds its three attributes (`seat_count`,
    `manual_limit_microusd`, `seat_rate_microusd`), "a fixed-size item cannot
    grow" is untrue of the shipped schema, and this document must not repeat
    it — and it becomes MORE untrue again once F2 classifies
    `pool_granted_microusd` and the aggregate cap. The REPLACEMENT wording is
    F1's design work; this test only refuses the false sentence's survival,
    so it is agnostic to exactly what F1 writes instead."""
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
    to be under 1024 bytes, and to state the EXACT margin (not merely mention
    one) below the boundary.

    The subject here is "the currently-declared row fits in one write unit",
    never a literal byte count: F1's declaration does not yet classify
    `pool_granted_microusd` or the aggregate cap (deliberately — see the
    module docstring's UPDATE), so `worst_case_pool_item_bytes()` returns the
    worst case of what is classified TODAY, and is expected to grow when F2
    lands. This test must keep passing across that growth without editing,
    because it never hardcodes the number — only F1's/F2's own function does.

    Fails today for the correct reason: F1's declaration module does not
    exist in THIS worktree (pre-F1 — confirmed real elsewhere, not merged
    here), so there is nothing to derive from and nothing for the document
    to match yet."""
    try:
        from dynamo import pool_row_schema  # type: ignore
    except ImportError:
        pytest.fail(
            "backend.dynamo.pool_row_schema (F1's closed-world pool-row "
            "declaration, S1 in SEAMS (the integration owner's seam-review document)) does not exist in this worktree "
            "yet. The gauge baseline, its alarm threshold, and this "
            "document's worst-case figure are all supposed to derive from "
            "it (CONTRACT-F4-claims (F4's contract document) amendment B1) — until it lands, "
            "there is nothing to derive from. (Confirmed to exist elsewhere, "
            "returning a live value as of F1's landing — but this worktree "
            "does not merge that code, so importing it here must still fail.)"
        )

    computed = pool_row_schema.worst_case_pool_item_bytes()
    assert computed < 1024, (
        f"F1's declared schema computes a worst-case pool item of {computed} "
        f"bytes, which is NOT under the 1,024-byte write-unit boundary — the "
        f"one-write-unit conclusion this whole part exists to keep honest "
        f"would be false. This is a finding for F1 (or F2, once its two "
        f"attributes are classified), not a wording fix for F4."
    )
    expected_margin = 1024 - computed

    text = _doc_text()
    figures = {int(m.group(1).replace(",", "")) for m in BYTE_FIGURE.finditer(text)}
    assert computed in figures, (
        f"F1's schema declaration currently computes a worst-case pool item "
        f"of {computed} bytes, but docs/design/ledger-hot-path.md does not "
        f"state that number — the document's figure must be DERIVED from "
        f"the declaration (re-run whenever the declaration changes, e.g. "
        f"when F2 classifies its two attributes), never independently chosen "
        f"or hand-reconstructed."
    )
    margin_match = MARGIN_MENTION.search(text)
    assert margin_match, (
        f"the document states a worst-case figure but not the margin below "
        f"the 1,024-byte boundary ({expected_margin} bytes at the "
        f"currently-declared schema) — R39a's 'Verified by' requires the "
        f"boundary to be stated, and a margin makes that boundary a "
        f"checkable number rather than a bare 'it fits'."
    )
    stated_margin = int((margin_match.group(1) or margin_match.group(2)).replace(",", ""))
    assert stated_margin == expected_margin, (
        f"the document states a margin of {stated_margin} bytes, but "
        f"1024 - worst_case_pool_item_bytes() is {expected_margin} bytes at "
        f"the currently-declared schema — the stated margin must be computed "
        f"from the SAME live figure as the worst-case number, not a separate "
        f"number that happened to be true once."
    )
