"""The verification plan of CONTRACT-claim-coverage, as tests.

Written before the change, so each one is a reproduction of the defect first and an
observation of the fix second — the pairing the contract asks for. A test added after
the code cannot show that the defect was ever there.

They are ordinary tests rather than a checklist because the contract's whole subject is
that a claim about the documents must be checkable, and a verification plan that is only
prose is the same defect one level up.

Three rounds of amendments withdrew or narrowed parts of the plan this file originally
tested, and this revision follows them:

  - B1/A3/C3 withdrew the exact registry count ("196 anchored, count asserted"). The
    standing set ratchet that replaced it lives in `contracts/claims/snapshot.json` and
    is checked by `test_claim_obligations_ratchet.py`, not here — this file no longer
    asserts a count.
  - Amendment 9 found that `records`/`ships` were never added to the lexicon (B3 was a
    judgement about the ORIGINAL plan's proposal, not a removal), so there is nothing to
    test being detected or excepted for either word.
  - Amendment 10 narrowed `every`/`all` to quantification over behaviour, so the lexicon
    now has an honest way to tell "every route enforces…" from "every table is
    provisioned in PAY_PER_REQUEST mode" — a distinction the original lexicon could not
    draw at all.
  - Amendments 11 and 12 added, then corrected, a third-party exception to the
    protected-subject floor: naming DynamoDB/Bedrock/Cognito is what a sentence about
    THIS gateway's own mechanism looks like, not evidence it is about somebody else.
  - A4 withdrew the natural-language conjunction detector, and B7 kept the compound
    README sentence unsplit. The old test asserting that a single-clause anchor on that
    sentence must fail was testing a rule that was withdrawn before it shipped; it is
    gone rather than repaired.
  - A5/B6 moved the lexicon, the file list and the subject names into
    `contracts/claims/config.json`, inside the same ratchet as the registry, so "the
    documented policy is the one that runs" is now a comparison against that file, not
    against prose in `CONTRACTS.md`.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from tests import test_claims_are_anchored as lint


ROOT = pathlib.Path(__file__).resolve().parents[1].parent


def _registry() -> list[dict]:
    return json.loads(
        (ROOT / "contracts" / "claims" / "anchored.json").read_text())["claims"]


def _config() -> dict:
    return json.loads((ROOT / "contracts" / "claims" / "config.json").read_text())


# --------------------------------------------------------------- item 1: lexicon


def test_behavioural_absolute_is_guarantee_shaped_configuration_absolute_is_not():
    """Item 1, as amendment 10 left it. Widening the lexicon by `every|all` to catch
    ordinary promises like "Every call is recorded as a structured JSON log" also
    caught statements of fact about configuration — "every table is provisioned in
    `PAY_PER_REQUEST` mode", "`cdk-nag` runs at every synth" — that are not promises
    about behaviour and had no honest anchor kind. Before amendment 10's restriction to
    a nearby behavioural verb, the detector could not tell these apart; a fix that only
    adds the quantifier back would fail this test's second half."""
    behavioural = (
        "Every route enforces per-user token quotas and optional per-tenant dollar "
        "pool and per-model budgets with atomic DynamoDB reservations."
    )
    configuration = "Every table is provisioned in `PAY_PER_REQUEST` mode."
    assert lint.GUARANTEE.search(behavioural), behavioural
    assert not lint.GUARANTEE.search(configuration), (
        f"{configuration!r} is a quantifier over configuration, not over behaviour, "
        "and matching it is the defect amendment 10 closed")


# ------------------------------------------------------------- item 2: policy identity


def test_the_documented_lexicon_is_the_one_that_runs():
    """Item 2, per A5/B6. The contract's answer to "you cannot prove a lexicon
    complete" is that the lexicon, the file list and the subject names live in one
    file a reader can read: `contracts/claims/config.json`. That claim is only true if
    the objects the lint module actually compiled at import time are built FROM that
    file, rather than a copy someone forgot to keep in sync — which is exactly the
    failure mode A5 as first written risked (a policy file nobody watches)."""
    cfg = _config()

    assert lint.COVERED == tuple(cfg["covered_documents"])
    assert lint.SUBJECT_NAMES == tuple(cfg["subject_names"])
    assert lint.GUARANTEE.pattern == "|".join(cfg["guarantee_terms"])

    # Falsifiability: the equalities above are not vacuously true. A pattern built
    # from one fewer term does not match what the lint compiled, so the check above
    # is sensitive to the two objects actually diverging.
    shrunk_pattern = "|".join(cfg["guarantee_terms"][:-1])
    assert shrunk_pattern != lint.GUARANTEE.pattern
    shrunk_covered = tuple(cfg["covered_documents"][:-1])
    assert shrunk_covered != lint.COVERED


# ------------------------------------------------------- item 3: the file list


def test_all_six_documents_are_covered_and_the_architecture_security_sentences_anchor():
    """Item 3. `docs/ARCHITECTURE.md` was unlinted, and a reader of it had no way to
    know whether "Credentials are never transmitted to Stratoclave" and "*never* reads
    `cognito:groups`" — two real security promises — were being checked at all, or
    silently exempt because their file was outside the fence."""
    for rel in ("README.md", "docs/SCOPE.md", "docs/design/hard-ceiling.md",
                "docs/ARCHITECTURE.md", "docs/ADMIN_GUIDE.md", "docs/DEPLOYMENT.md"):
        assert rel in lint.COVERED, rel

    # Negative case: the boundary this contract drew is "six documents, not every
    # document" (docs/MEASUREMENTS.md and the guides stay out, named in the open
    # items). If everything were covered, this assertion would be the only one in the
    # file that could never fail.
    assert "docs/MEASUREMENTS.md" not in lint.COVERED

    ids = lint._clause_ids()
    registry = _registry()
    for fragment in ("Credentials are never transmitted",
                      "never* reads `cognito:groups`"):
        hits = [c for c in registry if fragment in lint._normalize(c["claim"])]
        assert hits, f"no registered claim contains {fragment!r}"
        for c in hits:
            kind, clauses, _rest = lint.parse_anchor(str(c["anchor"]))
            assert kind == "contract", (
                f"{fragment!r} is a security guarantee and needs a clause that "
                f"exists, got anchor {c['anchor']!r}")
            for cid, _pin in clauses:
                assert cid in ids, (
                    f"{fragment!r} is anchored to {cid}, which is not a clause in "
                    "CONTRACTS.md")


# ------------------------------------------------- item 4: multi-clause anchors


def test_multi_clause_anchor_resolves_and_one_bad_id_inside_it_still_fails():
    """Item 4, proved against the lint's own parser and clause table directly rather
    than by depending on any one registry row, because the registry's multi-clause
    anchors are still being written by other work landing alongside this file. A list
    is only better than a single id if a single bad id inside it is still caught."""
    ids = lint._clause_ids()
    known = [cid for cid in ("C1.1", "C6.2", "C6.7", "C6.8") if cid in ids]
    assert len(known) >= 2, (
        "need at least two real clause ids fixed in CONTRACTS.md to exercise a "
        "multi-clause anchor")
    first, second = known[0], known[1]

    kind, clauses, _rest = lint.parse_anchor(f"contract:{first},{second}")
    assert kind == "contract"
    assert [cid for cid, _pin in clauses] == [first, second]
    assert all(cid in ids for cid, _pin in clauses)

    _kind2, clauses2, _rest2 = lint.parse_anchor(f"contract:{first},NOT-A-REAL-CLAUSE")
    unresolved = [cid for cid, _pin in clauses2 if cid not in ids]
    assert unresolved == ["NOT-A-REAL-CLAUSE"], (
        "a single unknown id inside an otherwise-valid multi-clause list must still "
        "be caught, not hidden by the valid id next to it")


# ------------------------------------------------------------- item 5: protected floor


def test_protected_subject_floor_holds_except_for_named_third_parties():
    """Item 5, reproducing the defect this rewrite exists to fix.

    This test used to re-derive C5's floor line by line — its own copy of "protected
    subject AND guarantee vocabulary AND not contract/debt/evidence AND not a named
    third party" — instead of calling the one the lint module runs
    (`lint.floor_requires_a_clause`). The floor gained two exemptions since: a sentence
    whose subject is the document itself (`document_scope_prefixes`), and an
    enumerated per-id list (`floor_exemptions`), both in `contracts/claims/config.json`.
    The copy here never learned about either, so it failed on the eleven live claims
    that carry those exemptions while the real rule passed them. Two implementations of
    one rule, drifting apart, is the exact defect shape the change contract is
    organised against.

    What this proves is narrower than "the rule holds" — `test_claims_are_anchored.py`
    already asserts that over the live registry, and it is not this file's job to
    assert it a second time. This proves the rule IS the one C5 describes: on
    synthetic sentences built here, a claim about OUR ledger is caught, a claim about
    LiteLLM's is not, and an exemption is a matter of an id being enumerated in the
    config rather than something a similarly-shaped sentence gets for free. Built from
    synthetic rows rather than the registry, so today's registry contents cannot make
    this pass or fail by accident.
    """
    ours = {
        "id": "cl-synthetic-ours",
        "claim": "The ledger reservation this gateway holds is never lost across a crash.",
        "anchor": "descriptive:synthetic negative case, not a real registry entry",
    }
    theirs = {
        "id": "cl-synthetic-theirs",
        "claim": "LiteLLM never persists the credential it is handed.",
        "anchor": "descriptive:synthetic negative case, not a real registry entry",
    }
    assert lint.floor_requires_a_clause(ours) is not None, (
        "a sentence naming OUR ledger, resting on descriptive:, must be caught by the "
        "floor")
    assert lint.floor_requires_a_clause(theirs) is None, (
        "a sentence naming a declared third party's credential must not be caught: "
        "the floor's exception exists for exactly this shape")

    # The exemptions are enumerated BY ID, not implicit in a sentence's shape. The same
    # sentence must pass only when its id is one of the ones named in
    # contracts/claims/config.json's `floor_exemptions`, and fail otherwise — proving
    # the exemption is a reviewable list, not a hole any similarly-shaped sentence can
    # climb through.
    cfg = lint._CFG
    exempt_id = next(iter(cfg.get("floor_exemptions", {})), None)
    assert exempt_id, "config.json's floor_exemptions is empty; nothing to prove against"
    shared_claim = "The tenant pool budget is never exceeded once admission is granted."
    exempt_row = {
        "id": exempt_id,
        "claim": shared_claim,
        "anchor": "descriptive:shares an id enumerated in floor_exemptions",
    }
    unlisted_row = {
        "id": "cl-not-in-the-exemption-list",
        "claim": shared_claim,
        "anchor": "descriptive:identical sentence, id not enumerated",
    }
    assert lint.floor_requires_a_clause(exempt_row) is None, (
        "an id present in floor_exemptions must be excused, regardless of the sentence")
    assert lint.floor_requires_a_clause(unlisted_row) is not None, (
        "the identical sentence under an id NOT in floor_exemptions must still be "
        "caught")


# ------------------------------------------------------------------ item 6: wording


def test_c10_1_states_the_lexicon_and_the_covered_file_list():
    """Item 6. C10.1 read as a claim about the documents ("every guarantee-shaped
    sentence in the covered documents is registered"), which is true of the vocabulary
    and silent about which documents it swept — the whole defect this contract closes.
    It has to say that "guarantee-shaped" means "matches the declared lexicon", and it
    has to name the file list rather than imply totality, because a claim of totality
    over "the documents" is the thing this contract replaced with a named boundary."""
    text = (ROOT / "docs" / "design" / "CONTRACTS.md").read_text()
    rows = [line for line in text.split("\n") if line.startswith("| **C10.1**")]
    assert rows, "C10.1 not found"
    row = rows[0]

    assert "lexicon" in row.lower() or "vocabulary" in row.lower(), (
        "C10.1 must say that guarantee-shaped means 'matches the declared lexicon', "
        "not merely 'every guarantee-shaped sentence'")
    assert "config.json" in row or "contracts/claims" in row, (
        "C10.1 must point at contracts/claims/config.json for the file list (A5), "
        "not enumerate or imply it inline")

    cfg = _config()
    tail = text.split("## Open items, named rather than implied", 1)[-1]
    for rel in cfg["uncovered_documents_named"]:
        assert pathlib.Path(rel).name in tail, (
            f"{rel} is outside the covered set and must be named BY NAME in the open "
            "items — a bare count of unswept documents goes stale without saying "
            "what is missing")
