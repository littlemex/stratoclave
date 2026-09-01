"""F4 / R39d — every published figure in ledger-latency.md states what schema it
was measured under, and whether it needs re-measuring after this epic.

WHAT DEFECT THIS CLOSES

The quota-raise epic changes the pool item's attribute set. A reader of
`docs/benchmarks/ledger-latency.md` has no way to tell, today, whether a given
number in that document describes the pool row as it exists NOW (post-epic)
or as it existed when the number was captured (pre-epic) — the document does
not name a schema or an item size next to any figure at all. Per
the F4 design note section 5, none of the EXISTING figures need re-measuring
because of this epic (none of them are sensitive to the pool item's byte
count — they measure transaction/round-trip latency), but every one of them
needs the annotation saying so explicitly, so a reader does not have to
independently reconstruct that reasoning or, worse, wrongly assume staleness
or wrongly assume freshness.

WHAT THIS TEST CHECKS

Each of the section headers this test names (the F4 design note's table) must be
followed, before the next `##`/`###` heading, by a short annotation block
naming: the item size or schema state at capture, and whether the figure was
re-measured after the epic (or explicitly marked as not needing to be).

WHY THIS FAILS TODAY

`docs/benchmarks/ledger-latency.md` has no such annotations anywhere — this
epic has not landed and nobody has gone through the document adding them.
docs/ is out of scope for F4 to edit (the F4 design note section 5 specifies the
exact annotation text per figure).

SEAM CORRECTION B2, then CORRECTED AGAIN (integration owner, second pass) — "F1 adds
three attributes, not two" was itself over-precise. `pool_granted_microusd` and
`grant_cap_microusd` are ABSENT BY DEFAULT (S2: absence means "derive from the
source"), not written as zero, so there is no single "post-epic attribute set" — a
pool row is in exactly ONE of three reachable shapes (see the F4 design note section
0b for the derivation):

  1. never granted        — `seat_count`, `manual_limit_microusd`, the stored seat
     rate; no `pool_granted_microusd`, no `grant_cap_microusd`.
  2. granted, cap derived — shape 1 plus `pool_granted_microusd`.
  3. granted, cap explicit (the WORST CASE) — shape 2 plus `grant_cap_microusd`.

A post-epic figure's annotation must therefore NAME WHICH SHAPE it measured, and list
ONLY that shape's attributes — naming all of `seat_count`/`manual_limit`/
`pool_granted`/`grant_cap`/the rate for a row that only carries three of them is the
same over-precision this part has now corrected twice, in the other direction (too
few, then too many).

SEAM CORRECTION B5 — the figure this test's second half polices, for the GRANTED
shapes only, is the COMPOSED re-run `CONTRACT-F4-claims (F4's contract document)`
calls out: "one re-run through [the fixture] after F3, with the figure difference
attributed either way, is the evidence R39d anchors to." That re-run needs F1's AND
F2's attributes present on the same row simultaneously (`limit == baseline + granted`
needs F2's `pool_granted`; the baseline itself needs F1's `seat_count`/
`manual_limit`) — a state none of F1/F2/F3 individually reaches in its own tests. A
never-granted-shape figure does not exercise F2's attribute at all, so this
requirement is scoped to the two granted shapes, not to every post-epic figure.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "benchmarks" / "ledger-latency.md"

#: Section headers the F4 design note's table names, and the annotation vocabulary each
#: one's following prose must contain. Deliberately loose (an "or" of a few
#: reasonable phrasings) so a reasonable human wording of the annotation passes,
#: while an absent annotation still fails.
SECTIONS = [
    ("### Floor — zero contention (concurrency = 1)",
     re.compile(r"(pre-epic|before.{0,20}epic|schema|attribute set)", re.IGNORECASE)),
    ("### Contention curve — single tenant pool row (worst case)",
     re.compile(r"(pre-epic|before.{0,20}epic|schema|attribute set)", re.IGNORECASE)),
    ("### After: headroom ADD gate (same worst case, re-measured)",
     re.compile(r"(pre-epic|before.{0,20}epic|schema|attribute set)", re.IGNORECASE)),
    ("## Item-count spike",
     re.compile(r"(item count.{0,40}not.{0,10}(byte|size)|byte size|unrelated to)",
                re.IGNORECASE)),
]


def _sections(text: str) -> dict[str, str]:
    """{heading text: the prose between it and the next heading of level <= its own}."""
    lines = text.split("\n")
    out: dict[str, str] = {}
    headings = [(i, line) for i, line in enumerate(lines) if line.startswith("#")]
    for idx, (line_no, heading) in enumerate(headings):
        end = headings[idx + 1][0] if idx + 1 < len(headings) else len(lines)
        out[heading.strip()] = "\n".join(lines[line_no:end])
    return out


def test_every_named_figure_states_its_schema_and_remeasurement_status():
    text = DOC.read_text()
    sections = _sections(text)
    missing: list[str] = []
    for heading_prefix, pattern in SECTIONS:
        matched_heading = next(
            (h for h in sections if h.lstrip("# ").startswith(heading_prefix.lstrip("# "))),
            None,
        )
        if matched_heading is None:
            missing.append(f"heading not found at all: {heading_prefix!r}")
            continue
        body = sections[matched_heading]
        if not pattern.search(body):
            missing.append(
                f"{matched_heading!r} has no schema/re-measurement annotation "
                f"matching {pattern.pattern!r}"
            )
    assert not missing, (
        "figures in docs/benchmarks/ledger-latency.md are missing the per-figure "
        "schema/re-measurement annotation R39d requires (the F4 design note section 5 "
        "has the exact text for each):\n  " + "\n  ".join(missing)
    )


#: The rate is matched loosely (F1 has not named it in a contract F4 may read) rather
#: than by an exact attribute name, the same hedge the F4 design note already uses for
#: `manual_limit`/`pool_granted`'s possible `_microusd` suffix. F1's three (seat_count,
#: manual_limit, the rate) are present under EVERY shape, so this is checked
#: unconditionally, unlike the two grant attributes below.
_RATE_MENTION = re.compile(r"seat.{0,20}rate|rate.{0,20}seat", re.IGNORECASE)

#: Exactly one of these three must be named — "post-epic" alone names no attribute
#: set, since the two grant attributes are absent by default, not zero (S2).
_SHAPE_PATTERNS = {
    "never_granted": re.compile(r"never[- ]granted", re.IGNORECASE),
    "granted_cap_derived": re.compile(r"granted,?\s*cap derived", re.IGNORECASE),
    "granted_cap_explicit": re.compile(r"granted,?\s*cap explicit", re.IGNORECASE),
}

#: Whether each grant attribute must be claimed PRESENT for a given shape. A
#: never-granted row carries neither; cap-derived carries pool_granted only;
#: cap-explicit carries both.
_SHAPE_GRANT_ATTR_PRESENCE = {
    "never_granted": {"pool_granted": False, "grant_cap": False},
    "granted_cap_derived": {"pool_granted": True, "grant_cap": False},
    "granted_cap_explicit": {"pool_granted": True, "grant_cap": True},
}

_ATTR_MENTION = {
    "pool_granted": re.compile(r"pool_granted", re.IGNORECASE),
    "grant_cap": re.compile(r"grant_cap", re.IGNORECASE),
}

#: A word within this many characters BEFORE a mention that turns "the text names
#: this attribute" into "the text names this attribute AS ABSENT" — e.g. "neither
#: pool_granted_microusd nor grant_cap_microusd exists" must not be read as claiming
#: either is present.
_NEGATION_WINDOW = 80
_NEGATION_WORDS = re.compile(
    r"\b(no|not|neither|nor|without|absent|does ?n[o']?t|omit(?:s|ted)?)\b",
    re.IGNORECASE,
)

#: B5: the composed re-run needs BOTH identities satisfied on the same row —
#: not just "a fixture was used". Loose phrasing so a reasonable human wording
#: passes; the point is that both halves are named, not the exact words. Only
#: required for the two GRANTED shapes (a never-granted row does not exercise
#: F2's attribute at all, so there is no composed state to attribute).
_BOTH_IDENTITIES_MENTION = re.compile(
    r"(both identities|f1.{0,10}and.{0,10}f2|baseline.{0,30}granted.{0,30}"
    r"headroom|after f3)",
    re.IGNORECASE,
)


def _claims_attribute_present(text: str, pattern: re.Pattern) -> bool:
    """True if `pattern` matches somewhere with no negation word in the
    `_NEGATION_WINDOW` characters immediately before the match — a heuristic for
    "the text describes this attribute as being ON the row", not merely mentioning
    its name (which a correct never-granted annotation does, to say it is absent)."""
    for match in pattern.finditer(text):
        window_start = max(0, match.start() - _NEGATION_WINDOW)
        if not _NEGATION_WORDS.search(text[window_start:match.start()]):
            return True
    return False


def test_a_post_epic_figure_names_its_shape_and_only_that_shapes_attributes():
    """The F4 design note, section 5, corrected twice (B2, then the integration
    owner's follow-up): any NEW figure captured after F1-F3 land must (1) say it
    went through `seed_verified_pool` (R39c's fixture), (2) name F1's three
    always-present attributes including the stored seat rate, (3) name EXACTLY
    ONE of the three reachable row shapes, (4) claim as present ONLY the grant
    attributes that shape actually carries — not all four/five for a row that
    carries two or three, which is the same over-precision this part corrected
    once already, in the opposite direction — and (5), for a GRANTED shape only,
    say the re-run satisfied both of the fixture's identities at once. Fails
    today because no post-epic figure exists in the document at all yet (there
    is nothing to check) — this test exists so that the day one is added, it is
    checked against the shape-aware requirement rather than a flat attribute
    list an earlier draft of this test accepted."""
    text = DOC.read_text()
    if "post-epic" not in text.lower() and "seat_count" not in text and (
            "pool_granted" not in text and "manual_limit" not in text):
        pytest.skip(
            "no post-epic figure exists in ledger-latency.md yet — nothing to "
            "check. This test starts enforcing the moment one is added."
        )
    assert "seed_verified_pool" in text, (
        "a post-epic figure was added to ledger-latency.md without naming "
        "seed_verified_pool (R39c's fixture) as how its seed row was verified."
    )
    assert _RATE_MENTION.search(text), (
        "a post-epic figure names the epic's attribute set but does not "
        "mention the stored seat rate (amendment A5) alongside "
        "seat_count/manual_limit — F1's three are present under every shape "
        "and the provenance must name all of them."
    )

    matched_shapes = [name for name, pat in _SHAPE_PATTERNS.items() if pat.search(text)]
    assert matched_shapes, (
        "a post-epic figure exists but does not name which of the three "
        "reachable row shapes (never granted / granted, cap derived / "
        "granted, cap explicit) it measured. 'post-epic' names no attribute "
        "set by itself: pool_granted_microusd and grant_cap_microusd are "
        "absent by default, not zero, so a reader cannot infer which of "
        "them the measured row actually carried."
    )
    assert len(matched_shapes) == 1, (
        f"a post-epic figure's annotation names more than one shape "
        f"({matched_shapes}) — a single measured row is in exactly one of "
        f"the three shapes; name the one that was actually measured."
    )
    shape = matched_shapes[0]

    problems: list[str] = []
    for attr, should_be_present in _SHAPE_GRANT_ATTR_PRESENCE[shape].items():
        claimed_present = _claims_attribute_present(text, _ATTR_MENTION[attr])
        if should_be_present and not claimed_present:
            problems.append(
                f"shape {shape!r} carries {attr}, but the annotation does not "
                f"claim it present")
        if not should_be_present and claimed_present:
            problems.append(
                f"shape {shape!r} does NOT carry {attr} (absent by default), "
                f"but the annotation claims it is present — listing an "
                f"attribute the measured row never had is the same "
                f"over-precision this part corrected once already, just in "
                f"the other direction")
    assert not problems, "\n  ".join([""] + problems)

    if shape != "never_granted":
        assert _BOTH_IDENTITIES_MENTION.search(text), (
            "a granted-shape post-epic figure does not state that its re-run "
            "satisfied BOTH of the fixture's identities at once "
            "(limit == baseline + granted, and "
            "headroom == limit - reserved - settled) — B5's composed state, "
            "which needs F1's and F2's attributes present simultaneously and "
            "is the evidence R39d is supposed to anchor to."
        )
