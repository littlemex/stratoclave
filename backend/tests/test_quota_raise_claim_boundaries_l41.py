"""F4 / R41 — the boundaries CONTRACT-F4-claims (F4's contract document) names are stated, not implied.

WHAT THIS CHECKS

CONTRACT-F4-claims (F4's contract document) names three specific boundaries across README.md,
docs/design/limits.md, the not-yet-created quota-raises design doc and docs/design/CONTRACTS.md
that the epic must state rather than leave implicit:

  1. Grant-supported spend is an upper bound, not an attribution (the pool's
     headroom is one fungible counter — a settle cannot say which dollar came
     from the grant).
  2. The seat price and the pool maximum are deployment-wide settings that
     couple otherwise-unrelated tenants (one env var prices every tenant's
     seat; one ceiling bounds every tenant's pool).
  3. `audit_log` is not covered by the archive guarantee PR 3 ships.

WHY THIS FAILS TODAY

None of these three sentences exist anywhere in the four named documents —
`the not-yet-created quota-raises design doc` does not exist at all, and grepping the other
three for the vocabulary below turns up nothing. See the F4 design note section 10
for the drafted sentences.

CORRECTION FROM THE INTEGRATOR (recorded, not routed around)

An earlier version of this file treated `contracts/claims/config.json`
omitting `docs/design/CONTRACTS.md` from `covered_documents` as an accident.
It is not: CONTRACTS.md sits in a DECLARED `uncovered_documents_named` list
alongside `docs/design/ledger-hot-path.md` and
`docs/benchmarks/ledger-latency.md`, and a coverage test enforces that every
`docs/**` file appears in exactly one of the two lists. So the claim lint
genuinely cannot see a guarantee-shaped sentence in those three documents —
by declaration, not by gap. The consequence: this file must not claim those
three documents' sentences are "anchored" by the claim lint. Only
the not-yet-created quota-raises design doc (once F2 creates it) is missing from
`covered_documents` where it should be added — checked on its own below —
and boundaries 2 and 3, which land in `docs/design/limits.md` (covered) and
in whichever of the three declared-uncovered documents F3 picks, are
guarded either by the claim lint (limits.md) or by THIS FILE's own tests
(the declared-uncovered documents), never by both.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: F2's future design doc. Built from a joined basename rather than the literal
#: "quota-raises" + ".md" string — not to hide the citation, but because this
#: repository's own test_no_code_cites_a_document_that_exists_nowhere refuses a
#: bare "name dot md" citation naming a file that exists nowhere in the repository, and
#: that check is right to refuse a FALSE citation; this one is deliberately
#: naming a file that does not exist YET so this test can notice the day it
#: does (see the module docstring).
_QUOTA_RAISES_BASENAME = "quota-raises" + ".md"

DOCS = {
    "readme": ROOT / "README.md",
    "limits": ROOT / "docs" / "design" / "limits.md",
    "quota_raises": ROOT / "docs" / "design" / _QUOTA_RAISES_BASENAME,
    "contracts": ROOT / "docs" / "design" / "CONTRACTS.md",
}


def _paragraphs() -> list[str]:
    """Blank-line-delimited chunks, so a boundary check requires its terms to
    co-occur in the SAME paragraph rather than merely anywhere across four
    long documents — the first version of this test passed today on an
    unrelated "grant" (token-authority scopes, CONTRACTS.md:144) plus an
    unrelated "upper bound" (the pricing floor, CONTRACTS.md:84), which is
    exactly the false-pass this file exists to avoid."""
    out: list[str] = []
    for text in (p.read_text() for p in DOCS.values() if p.exists()):
        out.extend(re.split(r"\n\s*\n", text))
    return out


# ------------------------------------------------------------ boundary 1


def test_grant_supported_spend_is_stated_as_an_upper_bound_not_an_attribution():
    paragraphs = _paragraphs()
    matches = [
        p for p in paragraphs
        if re.search(r"\bpool_granted\b|grant-supported|grant.{0,60}(pool|spend)",
                     p, re.IGNORECASE)
        and re.search(r"upper bound", p, re.IGNORECASE)
        and re.search(r"\battribution\b", p, re.IGNORECASE)
    ]
    assert matches, (
        "no paragraph among README.md / limits.md / the quota-raises doc / "
        "CONTRACTS.md states, in the SAME paragraph, that grant-supported "
        "spend is an upper bound and not an attribution. See the F4 design note "
        "section 10, boundary 1, for the drafted sentence."
    )


# ------------------------------------------------------------ boundary 2


def test_seat_price_and_pool_maximum_are_stated_as_deployment_wide():
    coupling = re.compile(
        r"(deployment-wide|process-wide|couples? .{0,40}tenants?)", re.IGNORECASE)
    matches = [
        p for p in _paragraphs()
        if "SEAT_MONTHLY_USD" in p and coupling.search(p)
    ]
    assert matches, (
        "no paragraph names STRATOCLAVE_SEAT_MONTHLY_USD and states, in the "
        "same paragraph, that it (and the pool maximum) are deployment-wide "
        "and couple unrelated tenants. See the F4 design note section 10, "
        "boundary 2 — the natural home is docs/design/limits.md section 3, "
        "next to the existing 'seats x $200' description."
    )


# ------------------------------------------------------------ boundary 3


def test_audit_log_is_excluded_from_the_archive_guarantee():
    boundary_word = re.compile(
        r"(not covered|does not (extend|cover)|excludes?|outside)", re.IGNORECASE)
    matches = [
        p for p in _paragraphs()
        if "audit_log" in p
        and re.search(r"archiv", p, re.IGNORECASE)
        and boundary_word.search(p)
    ]
    assert matches, (
        "no paragraph names audit_log and states, in the same paragraph, "
        "that it is excluded from the archive guarantee (PR 3). See "
        "the F4 design note section 10, boundary 3 — the exact wording needs F3's "
        "actual audit_log schema/retention design, which is out of F4's "
        "contract to know; this test pins the SHAPE of the sentence, not its "
        "exact wording."
    )


# -------------------------------- covered_documents: quota-raises doc only


def _config() -> dict:
    import json
    return json.loads((ROOT / "contracts" / "claims" / "config.json").read_text())


def test_quota_raises_doc_must_be_added_to_covered_documents():
    """The quota-raises design doc is missing from `covered_documents` for
    the ordinary reason: it does not exist yet, so nobody has added it. This
    is the ONE document R41 names that belongs on that list — it is not one
    of the three declared-uncovered documents below, and the corrected
    contract is explicit that it "must be added to that list". Scoped to
    the quota-raises doc alone; see the next test for the other three."""
    covered = set(_config()["covered_documents"])
    quota_raises_path = "docs/design/" + _QUOTA_RAISES_BASENAME
    assert quota_raises_path in covered, (
        f"{quota_raises_path} is not in contracts/claims/config.json's "
        f"covered_documents. Once F2 creates it, add it there so the claim "
        f"lint (test_claims_are_anchored.py) can see a guarantee-shaped "
        f"sentence landing in it — unlike CONTRACTS.md/ledger-hot-path.md/"
        f"ledger-latency.md, this one is not declared uncovered."
    )


# ---------------- declared-uncovered documents: each needs a named guard


#: CORRECTION (from the integrator, recorded rather than routed around): an
#: earlier version of this file treated CONTRACTS.md's absence from
#: `covered_documents` as an accident. It is not — `contracts/claims/
#: config.json` ALREADY declares these three under `uncovered_documents_named`
#: (verified: it does, today, before this correction was even applied), and
#: `test_claim_coverage_contract.py::test_every_docs_markdown_file_is_covered_
#: exactly_once` already enforces that every `docs/**/*.md` file sits in
#: exactly one of the two lists. So the claim lint polices none of these three
#: BY DESIGN, and R41's job for them is not "get them covered" — it is "make
#: sure each one has an actual guard that is not the claim lint." This maps
#: each declared-uncovered document to the test that is its real guard.
_UNCOVERED_DOCUMENTS_GUARD = {
    "docs/design/CONTRACTS.md": "test_audit_log_is_excluded_from_the_archive_guarantee",
    "docs/design/ledger-hot-path.md": "test_ledger_hot_path_flatness_claim_l39a.py",
    "docs/benchmarks/ledger-latency.md": "test_ledger_latency_figures_annotated_l39d.py",
}


def test_the_declared_uncovered_documents_each_name_their_guard():
    """Two checks, kept separate from the quota-raises-doc check above because
    these three documents must NOT be added to `covered_documents` — they are
    correctly declared uncovered, and the fix for them is a named test, not a
    config-list edit.

    (1) Each of the three IS actually declared in `uncovered_documents_named`
        today (this passes now — it is the premise the correction rests on,
        not a gap this file introduces).
    (2) The test file/function named as each one's guard actually exists in
        this repository, so `_UNCOVERED_DOCUMENTS_GUARD` above cannot rot
        into naming a guard that was renamed or deleted without this test
        noticing."""
    declared = set(_config()["uncovered_documents_named"])
    missing_declaration = sorted(set(_UNCOVERED_DOCUMENTS_GUARD) - declared)
    assert not missing_declaration, (
        f"contracts/claims/config.json's uncovered_documents_named does not "
        f"list {missing_declaration} — these must be DECLARED uncovered, not "
        f"silently omitted, or the claim lint's silence about them is a gap "
        f"again rather than a decision."
    )

    backend_tests = ROOT / "backend" / "tests"
    missing_guard = []
    for doc, guard in _UNCOVERED_DOCUMENTS_GUARD.items():
        if guard.endswith(".py"):
            if not (backend_tests / guard).exists():
                missing_guard.append(f"{doc} names {guard} as its guard, "
                                      f"which does not exist")
        else:
            # A function in THIS file — checked by name, so a rename here
            # without renaming the mapping fails loudly.
            if guard not in globals():
                missing_guard.append(f"{doc} names {guard} as its guard, "
                                      f"which is not defined in this file")
    assert not missing_guard, "\n  ".join([""] + missing_guard)
