"""A worklist for the two claim anchors that are judgements, not links.

`contract:`, `evidence:` and `debt:` anchors point at something else that was checked:
a clause with its own enforcing test, a row in EVIDENCE.md, an item under Open items.
`boundary:` and `descriptive:` do not point anywhere — they assert that the sentence
carries its own limit, or that it makes no claim about this gateway at all. Nothing in
`test_claims_are_anchored.py` verifies either assertion; it only verifies that the
anchor exists and, for `descriptive:`, that the sentence does not literally contain the
project's name.

That last check has a hole, named and left open on purpose in
`test_a_protected_subject_cannot_rest_on_a_judgement`: a sentence about a protected
subject (a ledger, a pool, a reservation, a credential…) is normally required to rest
on a clause, evidence, or a debt — except when it names a third party (LiteLLM, a
credential broker, Vault, vLLM, the Semantic Router) and not this project, because a
clause about somebody else's behaviour is exactly what this project declines to write.
The hole is that the exemption is checked by string match, not by meaning: a sentence
that is actually about THIS gateway's own ledger, written to also mention a competitor
by name, passes the same way a sentence genuinely about the competitor does. No lexicon
closes that, because "is this sentence about us" is the judgement the anchor exists to
avoid re-deriving mechanically.

WHAT THIS MODULE BUYS, AND WHAT IT DOES NOT

It cannot tell the honest exemptions from the gamed ones. What it buys is that the
population that goes through the hole is enumerable rather than invisible, so a human
doing the standing review of `boundary:`/`descriptive:` claims has a worklist instead of
a haystack, and can read nine sentences instead of rereading three hundred. A worklist
is not a gate: sections 1 and 2 below print their population and assert only the
mechanical sanity of the selection, never a count, because a count is a target that
rephrasing satisfies without fixing anything. Section 3 asserts what it can: that a
`boundary:` reason which quotes a specific limit is quoting something that is actually
in the sentence, because a reason is not evidence if the limit it names cannot be found
in the claim it is attached to.
"""
from __future__ import annotations

import re

from tests import test_claims_are_anchored as lint


# --------------------------------------------------------------------- shared helpers


def _registry() -> list[dict]:
    return lint._registry()["claims"]


def _names_project(claim: str) -> list[str]:
    """Same rule `test_every_anchor_resolves` applies to `descriptive:` claims: a code
    span doesn't count, and a sentence whose subject is the document itself is allowed
    to say the project's name without that being a claim about its behaviour."""
    lowered = re.sub(r"`[^`]*`", " ", claim).lower()
    named = [n for n in lint.SUBJECT_NAMES if n in lowered]
    prefixes = tuple(lint._CFG.get("document_scope_prefixes", ()))
    if named and claim.startswith(prefixes):
        named = []
    return named


def _strip_markup(text: str) -> str:
    """Enough markdown removed that a quoted fragment and its source sentence compare
    on words, not on which one happened to carry the bold/backtick/link syntax."""
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[`*_>]", " ", text)
    return text


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", _strip_markup(text)).strip().lower()


def _quoted_fragments(reason: str) -> list[str]:
    """Substrings a reason presents as lifted from the sentence, via either quoting
    convention actually used in the registry."""
    return re.findall(r"'([^']+)'", reason) + re.findall(r"`([^`]+)`", reason)


# ------------------------------------------------------------ section 1: the exemption


def _exemption_worklist() -> list[dict]:
    """Every registered claim that hits the protected-subject floor in
    `test_a_protected_subject_cannot_rest_on_a_judgement` and passes only because the
    `descriptive:`-and-third-party exemption applies. Reproduces that test's own branch
    rather than re-deriving a new definition of "exempt", so this worklist and that
    floor cannot silently drift apart.
    """
    rows: list[dict] = []
    for c in _registry():
        claim = lint._normalize(c["claim"])
        kind, _clauses, _rest = lint.parse_anchor(str(c["anchor"]))
        if kind in ("contract", "debt", "evidence"):
            continue
        subject = lint.PROTECTED.search(claim)
        if not (subject and lint.GUARANTEE.search(claim)):
            continue
        if kind != "descriptive":
            continue
        third = lint.THIRD_PARTY.search(claim)
        if not third:
            continue
        rows.append({
            "id": c["id"],
            "protected": subject.group(0),
            "third_party": third.group(0),
            "claim": claim,
        })
    rows.sort(key=lambda r: r["id"])
    return rows


def test_the_exemption_worklist_is_printed_and_sanity_checked():
    """The population named in this module's docstring, printed for the standing
    review this test does not replace.

    No assertion here bounds the size of the list — a shrinking-count assertion is
    satisfied by rephrasing a sentence below the detector, which is the exact failure
    the docstring names. What IS asserted is that the selection is honest on its own
    terms: every row really does name a third party (the reason it was let through)
    and really does not name this project by the same rule `descriptive:` claims are
    always held to. If either check ever fails, the worklist has started silently
    filling with claims that were not actually exempt for the stated reason, which is
    worse than a haystack because it looks curated.
    """
    rows = _exemption_worklist()
    print(f"\nexemption worklist: {len(rows)} claim(s) pass the protected-subject "
          "floor only because they name a third party\n")
    for r in rows:
        print(f"  {r['id']}  protected={r['protected']!r}  "
              f"third_party={r['third_party']!r}\n    {r['claim']}")

    mislabelled = [
        r["id"] for r in rows
        if not lint.THIRD_PARTY.search(r["claim"]) or _names_project(r["claim"])
    ]
    assert not mislabelled, (
        "rows in the exemption worklist that do not actually name a third party, or "
        f"that also name this project by the descriptive: rule: {mislabelled}"
    )


# ------------------------------------------------------------- section 2: the judgement


def _document_claim_ids() -> dict[str, set[str]]:
    return {
        rel: {lint.claim_id(s) for s in sentences}
        for rel, sentences in lint.candidates().items()
    }


def test_the_judgement_census_is_printed_per_document():
    """The per-document share of `boundary:`/`descriptive:` claims, printed for a human
    to read — not a gate. A document that is mostly operational prose (a deployment
    guide, a hard-ceiling design doc walking through modes) will legitimately carry a
    high share of judgement anchors, because most of its guarantee-shaped sentences
    are themselves stating the limit they operate under. A uniform per-document ceiling
    would either fail honestly-scoped documents or invite claims to be moved to whichever
    document has slack left, so the only assertion here is the ratchet that already
    exists in `test_the_weaker_anchors_stay_a_minority` — re-derived directly against the
    registry rather than trusted from that test, since that test file is red right now
    for reasons this module does not depend on.
    """
    registry = _registry()
    by_id = {c["id"]: c for c in registry}
    doc_ids = _document_claim_ids()

    print("\njudgement census by document (boundary/descriptive share, human review "
          "only — not a gate):\n")
    for rel in lint.COVERED:
        ids = doc_ids.get(rel, set())
        doc_claims = [by_id[i] for i in ids if i in by_id]
        total = len(doc_claims)
        judgement = sum(
            1 for c in doc_claims
            if lint.parse_anchor(str(c["anchor"]))[0] in ("boundary", "descriptive")
        )
        share = judgement / total if total else 0.0
        print(f"  {rel}: {judgement}/{total} ({share:.0%})")

    if not registry:
        return
    for kind in ("boundary", "descriptive"):
        share = sum(1 for c in registry if str(c["anchor"]).startswith(kind)) / len(registry)
        print(f"  [registry-wide] {kind}: {share:.0%}")
        assert share <= 0.60, (
            f"{share:.0%} of the whole registry rests on {kind}: anchors — anchor them "
            "to contract clauses or evidence rows, or delete the claims"
        )


# --------------------------------------------------------------- section 3: the reasons


_BARE = {"boundary", "descriptive", "a boundary", "a descriptive", "is boundary",
         "is descriptive", "it is boundary", "it is descriptive", "this is boundary",
         "this is descriptive", "a boundary anchor", "a descriptive anchor"}


def _is_bare_restatement(kind: str, reason: str) -> bool:
    flat = re.sub(r"[^a-z ]", "", reason.strip().lower())
    flat = re.sub(r"\s+", " ", flat).strip()
    return flat in _BARE or flat == kind


def test_boundary_and_descriptive_reasons_are_not_degenerate():
    """The only mechanical check available on a judgement's honesty.

    `boundary:` and `descriptive:` anchors carry a free-text reason instead of a
    pointer, so nothing stops the reason from being empty, or from being the anchor
    kind typed out in words instead of an actual reason. Both are asserted against
    directly. The one further check that is real rather than cosmetic: when a
    `boundary:` reason quotes a specific phrase — the mechanism by which this registry
    actually cites a limit — that phrase has to be findable in the sentence it is
    attached to, once markdown emphasis and link syntax are normalised away. A reason
    that quotes nothing makes no checkable claim and is left to the human review in
    section 2; a reason that quotes something absent from its own sentence is not a
    boundary, it is a caption someone wrote once and never re-read against the text.

    If this fails against real data, the fix is to correct or re-anchor the flagged
    claim — not to loosen this check, because the whole point of the exercise is that
    the check stays real.
    """
    empty: list[str] = []
    bare: list[str] = []
    unfindable: list[str] = []

    for c in _registry():
        kind, _clauses, rest = lint.parse_anchor(str(c["anchor"]))
        if kind not in ("boundary", "descriptive"):
            continue
        reason = rest.strip()
        if not reason:
            empty.append(c["id"])
            continue
        if _is_bare_restatement(kind, reason):
            bare.append(c["id"])
            continue
        if kind == "boundary":
            claim = lint._normalize(c["claim"])
            for frag in _quoted_fragments(reason):
                if _norm(frag) not in _norm(claim):
                    unfindable.append(
                        f"{c['id']}: reason quotes {frag!r}, not found in "
                        f"sentence: {claim[:160]}")

    assert not empty, f"boundary:/descriptive: anchors with an empty reason: {empty}"
    assert not bare, (
        f"boundary:/descriptive: anchors whose reason just restates the anchor kind: "
        f"{bare}"
    )
    assert not unfindable, (
        "boundary: reasons that quote a limit absent from their own sentence:\n  "
        + "\n  ".join(unfindable)
    )
