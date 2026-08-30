"""C10 — every guarantee-shaped sentence in the covered documents has an anchor.

The contract's opening rule is that a clause with no test is a statement about one
commit rather than about the project. C10 — "every guarantee in the public documents
is true of the shipped code, or states the boundary at which its evidence stops" —
was the one clause that had no test, so it was exactly that: a statement about the
commit where someone last read the README.

WHAT IT ENFORCES

A sentence in a covered document that uses guarantee vocabulary must be registered
in `contracts/claims/anchored.json` with an anchor saying WHY it is allowed to say
that. Five anchor kinds, each checked differently:

  contract:<clause>[,<clause>…]  The clauses exist in docs/design/CONTRACTS.md and
                      carry their guarantee level and enforcing test. A list is for a
                      compound sentence: one clause per conjunct. Each id may carry
                      `@<pin>` fixing the clause's own words, so editing a clause
                      breaks the anchors resting on it.
  evidence:<text>     The text appears in docs/EVIDENCE.md, which is where a claim's
                      evidence — and the point it runs out — is recorded.
  boundary:<why>      The sentence carries its own limit, and the lint requires a
                      boundary word IN the sentence.
  descriptive:<why>   The vocabulary matched but the sentence makes no claim about
                      this gateway. Requires a reason, and refuses a sentence that
                      names the subject, so it cannot become a silent escape hatch.
  debt:<clause>       True AS QUALIFIED, and the named clause is what would make it
                      unconditional. The clause must exist and be listed under "Open
                      items", so a softened sentence and the work that would
                      strengthen it are the same list.

WHERE THE POLICY LIVES

The lexicons, the covered file list, the subject names and the protected subjects are
in `contracts/claims/config.json`, not in this file. A reader can see what is being
checked without reading a test, and the config is inside the same ratchet as
everything else — because this whole apparatus exists to stop a trusted file from
being the file nobody watches, and exempting the policy file would have been that
same defect one level down.

WHAT IT DOES NOT DO

It cannot tell whether a sentence is true — only whether someone was made to point at
the reason. Whether a clause is true, whether a sentence needs a `contract:` anchor,
whether a cited test enforces its clause, and whether a retirement preserved a claim
are all human readings, named as permanent obligations in `docs/design/CONTRACTS.md`.
That is the honest limit, and it is still the difference between "we are careful" and
"carelessness fails the build".
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[2]
CLAIMS_DIR = ROOT / "contracts" / "claims"
REGISTRY = CLAIMS_DIR / "anchored.json"
CONFIG = CLAIMS_DIR / "config.json"
CONTRACTS = ROOT / "docs" / "design" / "CONTRACTS.md"


def config() -> dict:
    return json.loads(CONFIG.read_text())


_CFG = config()

#: Vocabulary that turns a sentence into a promise. Deliberately broad: a false
#: positive costs one registry line with `descriptive:`, a false negative is an
#: unexamined guarantee. Each term in the config carries its own word boundaries; a
#: single wrapper around the alternation silently broke every prefix term, because no
#: boundary can follow the "c" of "enforc" when the text reads "enforces". `records`
#: and `ships` are absent rather than merely unused — see
#: `guarantee_terms_deliberately_absent` in the config.
GUARANTEE = re.compile("|".join(_CFG["guarantee_terms"]), re.IGNORECASE)

#: Words that make a claim carry its own limit. A `boundary:` anchor requires one.
BOUNDARY = re.compile(r"(?:" + "|".join(_CFG["boundary_terms"]) + r")", re.IGNORECASE)

#: This design document scopes a statement by naming the mode it applies to, which is
#: a limit as real as the word "unless". A `boundary:` anchor is satisfied by either.
MODE = re.compile(r"`(accounting|measured|shadow|enforced|strict|calibrated)`")

#: Subjects where a wrong claim costs something. See
#: `test_a_protected_subject_cannot_rest_on_a_judgement`.
PROTECTED = re.compile(
    r"(?:" + "|".join(_CFG["protected_subjects"]) + r")", re.IGNORECASE)

#: Somebody else's subject. A sentence naming one of these, and not naming us, is about
#: them — so the protected-subject floor does not apply to it, because a clause about a
#: third party's behaviour is the thing this project declines to write.
THIRD_PARTY = re.compile(
    r"(?:" + "|".join(_CFG["third_party_subjects"]) + r")", re.IGNORECASE)

COVERED = tuple(_CFG["covered_documents"])
MIN_LEN = int(_CFG["min_sentence_length"])
SUBJECT_NAMES = tuple(_CFG["subject_names"])

#: A sentence qualified BECAUSE the unconditional version is not true yet is a debt,
#: not a boundary. Without this, `boundary:` absorbs every unbuilt thing and C10.2 —
#: "a weakened sentence and the work that would strengthen it are the same list" — is
#: satisfied vacuously.
NOT_YET = re.compile(
    r"(not (yet )?built|not yet|is still unfixed|until .{0,40}(lands|exists|has run)|"
    r"remaining work|waiting to be written|does not yet|no recovery path|"
    r"kept as the interim)",
    re.IGNORECASE,
)

#: Words that name a proof. A sentence using them must say WHAT is proven and where
#: the proof stops, because "a formally-proven ledger" reads as a claim about the
#: shipped ledger while the proof is over a transition model written in a test file.
PROOF = re.compile(r"(formally[- ]proven|z3[- ]proven|proven ledger|provable ledger)",
                   re.IGNORECASE)
PROOF_BOUNDARY = re.compile(
    r"(model|modelled|transition|not over|outside the proof|boundary|deployment|"
    r"harness|invariant)",
    re.IGNORECASE,
)


def _units(text: str) -> list[str]:
    """Whole sentences, and table cells as sentences of their own.

    Prose here is hard-wrapped, so a naive per-line split yields fragments — and a
    registry keyed on fragments would fail the build every time someone re-wrapped a
    paragraph, which teaches authors to weaken the lint. Wrapped lines are joined
    into their paragraph first and only then split on sentence ends.

    A blockquote is prose wrapped the same way, one `>`-prefixed line per column
    width, so it gets the same treatment through its own accumulator: the marker is
    stripped and consecutive quoted lines are joined into one paragraph before the
    sentence split, rather than flushed line-by-line. Folding `>` into the "flush and
    emit this line verbatim" branch alongside headings and rules — which really are
    one line per unit — is what produced a registry entry that was not a sentence: a
    guarantee word landing on one hard-wrapped line of a multi-line quote registered
    that line alone, with the rest of its own sentence sitting mute on the lines
    before and after it.

    A table cell is its own unit because a verdict word in a comparison table is a
    claim by itself: that is how "Crash-safe budget accounting: Yes" came to be
    contradicted three sentences into its own cell.
    """
    out: list[str] = []
    paragraph: list[str] = []
    quote: list[str] = []
    in_fence = False

    def flush_paragraph() -> None:
        if paragraph:
            joined = " ".join(paragraph)
            out.extend(re.split(r"(?<=[.!?])\s+", joined))
            paragraph.clear()

    def flush_quote() -> None:
        if quote:
            joined = " ".join(quote)
            out.extend(re.split(r"(?<=[.!?])\s+", joined))
            quote.clear()

    for line in text.split("\n"):
        stripped = line.strip()
        # A fenced block is code, not a sentence. Its lines used to be joined into
        # the surrounding paragraph, which turned a list of `make` targets into a
        # guarantee-shaped "sentence" naming the gateway.
        if stripped.startswith("```"):
            flush_paragraph()
            flush_quote()
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if stripped.startswith("|"):
            flush_paragraph()
            flush_quote()
            out += [c.strip() for c in line.split("|") if c.strip()]
            continue
        if stripped.startswith(">"):
            # A continuation of the same quoted paragraph, not a new unit — see the
            # docstring above.
            flush_paragraph()
            quote.append(re.sub(r"^>\s?", "", stripped))
            continue
        if not stripped or stripped.startswith(("#", "---")):
            flush_paragraph()
            flush_quote()
            if stripped:
                out.append(stripped)
            continue
        # A list item starts a new unit; a continuation line extends the current one.
        if re.match(r"^([-*+]|\d+\.)\s", stripped):
            flush_paragraph()
        flush_quote()
        paragraph.append(stripped)
    flush_paragraph()
    flush_quote()
    return [u.strip() for u in out if u.strip()]


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def claim_id(text: str) -> str:
    """A stable identity for a claim, derived from the sentence that carried it.

    The identity is the point. A registry keyed on sentence text alone made the
    obligation disappear whenever the sentence did — so a rule like "the unanchored
    count may only decrease" was satisfiable by deleting a claim, rephrasing it below
    the detector, shortening it under the minimum length, or moving it into a table.
    An id survives all of those: the sentence is evidence locating an obligation,
    not the obligation itself.
    """
    return "cl-" + hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()[:12]


def candidates() -> dict[str, list[str]]:
    """{relative path: [claim sentence, ...]} over the covered documents."""
    found: dict[str, list[str]] = {}
    for rel in COVERED:
        text = (ROOT / rel).read_text()
        found[rel] = [
            _normalize(u) for u in _units(text)
            if GUARANTEE.search(u) and len(u) >= MIN_LEN
        ]
    return found


def _registry() -> dict:
    if not REGISTRY.exists():
        return {"claims": []}
    return json.loads(REGISTRY.read_text())


def _clause_rows() -> dict[str, str]:
    """{clause id: the whole row it lives on}, for existence and for pinning."""
    rows: dict[str, str] = {}
    for line in CONTRACTS.read_text().split("\n"):
        m = re.match(r"^\|\s*\*\*(C\d+(?:\.\d+[a-c]?)?)\*\*", line.strip())
        if m:
            rows[m.group(1)] = _normalize(line)
    return rows


def _clause_ids() -> set[str]:
    return set(re.findall(r"\*\*(C\d+(?:\.\d+[a-c]?)?)\*\*", CONTRACTS.read_text()))


def clause_pin(row: str) -> str:
    """The characters an anchor may carry to pin a clause's own words.

    Normalised for case, whitespace and punctuation, which deletes the churn a typo
    fix would otherwise cause. Deliberately no further: a one-word edit MUST break the
    anchors resting on it, because "never" → "not by default" is a typo-sized
    substantive change and no mechanism can tell a spelling fix from a weakening at the
    word level. Word-level breaks are the product, not the churn.
    """
    flat = re.sub(r"[^a-z0-9 ]+", " ", row.lower())
    return hashlib.sha256(re.sub(r"\s+", " ", flat).strip().encode()).hexdigest()[:8]


def parse_anchor(anchor: str) -> tuple[str, list[tuple[str, str]], str]:
    """`(kind, [(clause id, pin or "")], rest)`.

    `contract:C1.1@ab12cd34,C6.2` parses to two clauses with their pins; every other
    kind returns an empty clause list and its reason as `rest`.
    """
    kind, _, rest = anchor.partition(":")
    if kind not in ("contract", "debt"):
        return kind, [], rest
    clauses: list[tuple[str, str]] = []
    for part in rest.split(","):
        cid, _, pin = part.strip().partition("@")
        if cid.strip():
            clauses.append((cid.strip(), pin.strip()))
    return kind, clauses, rest


# --------------------------------------------------------------------- the checks


def test_every_claim_sentence_is_registered():
    """A guarantee-shaped sentence with no registry entry fails the build.

    The fix is never to soften the lint: it is either to anchor the sentence, to state
    its boundary in the sentence, or to delete the claim — and deleting it leaves the
    obligation behind by claim id, which is what `claim_id` exists for.
    """
    reg = {c["id"] for c in _registry()["claims"]}
    unregistered: list[str] = []
    for rel, claims in candidates().items():
        for claim in claims:
            if claim_id(claim) not in reg:
                unregistered.append(f"{rel}: {claim_id(claim)} {claim[:140]}")
    assert not unregistered, (
        f"{len(unregistered)} guarantee sentences have no anchor in "
        f"contracts/claims/anchored.json:\n  " + "\n  ".join(unregistered[:40])
    )


def test_every_registry_entry_carries_its_own_id():
    """An entry whose id does not derive from its text is one someone edited without
    re-examining, or two claims sharing a row."""
    bad = [
        c.get("id") for c in _registry()["claims"]
        if c.get("id") != claim_id(c["claim"])
    ]
    assert not bad, f"entries whose id does not derive from their sentence: {bad}"


def test_no_registry_entry_is_stale():
    """A registry entry whose sentence no longer exists is a claim someone edited or
    removed. Remove the entry in the same change as the edit."""
    live = {claim_id(c) for claims in candidates().values() for c in claims}
    stale = [
        f'{c["id"]} {c["claim"][:120]}' for c in _registry()["claims"]
        if c["id"] not in live
    ]
    assert not stale, (
        "registry entries no longer present in the documents:\n  " + "\n  ".join(stale)
    )


def test_every_anchor_resolves():
    """An anchor that points at nothing is not an anchor."""
    rows = _clause_rows()
    ids = _clause_ids()
    evidence = (ROOT / "docs" / "EVIDENCE.md").read_text()
    open_items = CONTRACTS.read_text().split(
        "## Open items, named rather than implied", 1)[-1]
    problems: list[str] = []
    for c in _registry()["claims"]:
        anchor = str(c.get("anchor", ""))
        claim = _normalize(c["claim"])
        kind, clauses, rest = parse_anchor(anchor)
        if kind == "contract":
            if not clauses:
                problems.append(f"contract anchor with no clause — {claim[:80]}")
            for cid, pin in clauses:
                if cid not in ids:
                    problems.append(
                        f"{cid} is not a clause in CONTRACTS.md — {claim[:80]}")
                elif pin and cid in rows and pin != clause_pin(rows[cid]):
                    problems.append(
                        f"{cid}'s wording changed since this sentence was anchored to it "
                        f"(pin {pin}, now {clause_pin(rows[cid])}). Re-read both and "
                        f"re-pin — {claim[:80]}")
        elif kind == "evidence":
            if rest not in evidence:
                problems.append(f"{anchor} does not appear in EVIDENCE.md — {claim[:80]}")
        elif kind == "boundary":
            if not (BOUNDARY.search(claim) or MODE.search(claim)):
                problems.append(
                    f"boundary anchor but the sentence states no limit — {claim[:120]}")
            if not rest.strip():
                problems.append(f"boundary anchor with no reason — {claim[:80]}")
        elif kind == "descriptive":
            if not rest.strip():
                problems.append(f"descriptive anchor with no reason — {claim[:80]}")
            # The project name inside a code span is a command, not a subject.
            lowered = re.sub(r"`[^`]*`", " ", claim).lower()
            named = [n for n in SUBJECT_NAMES if n in lowered]
            # A sentence whose SUBJECT is the document may name the project: "This document
            # describes … the invariants Stratoclave relies on" asserts nothing about
            # behaviour. It has to begin that way, which is not a form anyone reaches for by
            # accident.
            if named and claim.startswith(tuple(_CFG.get("document_scope_prefixes", ()))):
                named = []
            if named:
                problems.append(
                    f"descriptive anchor but the sentence names {named[0]!r}, so it is a "
                    f"claim about this gateway — {claim[:100]}")
        elif kind == "debt":
            for cid, _pin in clauses:
                if cid not in ids:
                    problems.append(
                        f"{cid} is not a clause in CONTRACTS.md — {claim[:80]}")
                elif cid not in open_items:
                    problems.append(
                        f"debt:{cid} is not named under Open items, so the weakened "
                        f"wording is not tracked as work — {claim[:80]}")
            if not str(c.get("note", "")).strip():
                problems.append(
                    f"debt anchor with no note saying what would close it — {claim[:80]}")
        else:
            problems.append(f"unknown anchor kind {anchor!r} — {claim[:80]}")

        if kind in ("contract", "boundary") and NOT_YET.search(claim):
            problems.append(
                f"the sentence says the thing is not built yet, so it is a debt and "
                f"needs `debt:<clause>` naming what would close it — {claim[:110]}")
        if PROOF.search(claim) and not PROOF_BOUNDARY.search(claim):
            problems.append(
                f"names a proof without saying what is proven or where the proof stops "
                f"— {claim[:110]}")
    assert not problems, "\n  ".join([""] + problems)


def floor_requires_a_clause(claim_row: dict) -> str | None:
    """The protected-subject floor (C5), as a predicate over one registry row.

    Returns the protected word matched (e.g. `"budget"`, `"ledger"`) when the row's
    claim uses guarantee vocabulary AND names a subject where a wrong claim costs
    something, and neither exemption applies — meaning the row must rest on
    `contract:`, `evidence:`, or `debt:`, and may not rest on `descriptive:` or a bare
    `boundary:`. Returns `None` when the row may rest on a judgement: it names no
    protected subject, carries no guarantee vocabulary, already anchors to a
    clause/evidence/debt, names a declared third party rather than us, its subject is
    the document itself (`document_scope_prefixes`), or its id is one of the
    enumerated `floor_exemptions` in `contracts/claims/config.json`.

    This is the single implementation of the floor. `test_claim_coverage_contract.py`
    calls it rather than re-deriving the rule, because two implementations of one rule
    is exactly the defect this whole apparatus exists to catch one level down.
    """
    claim = _normalize(claim_row["claim"])
    kind, _clauses, _rest = parse_anchor(str(claim_row["anchor"]))
    if kind in ("contract", "debt", "evidence"):
        return None
    subject = PROTECTED.search(claim)
    if not (subject and GUARANTEE.search(claim)):
        return None
    if kind == "descriptive" and THIRD_PARTY.search(claim):
        # About somebody else. The `descriptive:` rule already refuses a sentence
        # that names US, so passing both means it names them and not us.
        return None
    if claim.startswith(tuple(_CFG.get("document_scope_prefixes", ()))):
        # The subject is the document, not the system.
        return None
    if claim_row.get("id") in _CFG.get("floor_exemptions", {}):
        # Enumerated, with a reason, in the config. An exemption list is what was
        # chosen over widening the floor for everyone: a list is reviewable and a
        # widened rule is not, and `test_claim_judgement_worklist.py` prints these so
        # the population does not become invisible.
        return None
    return subject.group(0)


def test_a_protected_subject_cannot_rest_on_a_judgement():
    """The floor under C10's human half.

    A sentence that uses guarantee vocabulary AND names a subject where a wrong claim
    costs something — a credential, a budget, an admission, a ledger, an identity —
    may not be filed as `descriptive:` or as a bare `boundary:`. It has to point at a
    clause, an evidence row, or a debt.

    This exists because the class decides whether a focused enforcement test must exist
    at all, which makes a misfiling the cheapest way to skip the most expensive artifact
    in the system. It is a floor and not a detector: a claim about a protected subject
    written without any of these words still slips past, and no lexicon fixes that.
    What sits above the floor is a human judgement, said plainly in CONTRACTS.md.
    """
    problems: list[str] = []
    for c in _registry()["claims"]:
        word = floor_requires_a_clause(c)
        if word is None:
            continue
        claim = _normalize(c["claim"])
        kind, _clauses, _rest = parse_anchor(str(c["anchor"]))
        problems.append(
            f"{c['id']} names {word!r} and rests on {kind!r}; a guarantee "
            f"about that subject needs a clause, an evidence row, or a debt — "
            f"{claim[:110]}")
    assert not problems, "\n  ".join([""] + problems)


def test_the_registry_is_not_a_wildcard():
    """Every entry names one sentence, verbatim."""
    bad = [
        c.get("claim") for c in _registry()["claims"]
        if not str(c.get("claim", "")).strip()
        or len(_normalize(c["claim"])) < MIN_LEN
    ]
    assert not bad, f"registry entries that are not a single full sentence: {bad}"


@pytest.mark.parametrize("kind", ["descriptive", "boundary"])
def test_the_weaker_anchors_stay_a_minority(kind: str):
    """A ratchet on the ratchet.

    `descriptive:` and `boundary:` are judgements rather than links, so they are the
    two an author under time pressure would reach for. The threshold is not a target —
    it is the point at which this test asks for the claims to be anchored to clauses
    instead.
    """
    claims = _registry()["claims"]
    if not claims:
        pytest.skip("registry is empty")
    share = sum(1 for c in claims if str(c["anchor"]).startswith(kind)) / len(claims)
    assert share <= 0.60, (
        f"{share:.0%} of registered claims use {kind}: — anchor them to contract "
        "clauses or evidence rows, or delete the claims"
    )


def test_every_debt_is_visible_as_work():
    """The debts are the project's own challenge list, so they are printed rather than
    merely permitted."""
    debts = [c for c in _registry()["claims"] if str(c["anchor"]).startswith("debt:")]
    for c in debts:
        print(f"debt {parse_anchor(c['anchor'])[1]}: {c['claim'][:110]}")
    assert all(parse_anchor(c["anchor"])[1] for c in debts)


def test_a_hard_wrapped_blockquote_sentence_comes_out_whole():
    """Pins the class of bug this fixed: a `>` line was flushed and emitted verbatim,
    never joined with the quoted lines around it and never split on sentence ends —
    unlike a plain paragraph, which gets both. A guarantee word landing on one
    hard-wrapped line of a multi-line quote used to register that lone line, with the
    rest of its own sentence sitting mute before and after it.
    """
    text = (
        "> but *advisory\n"
        "> only*: it records a counterfactual and never steers execution, routing, or\n"
        "> money). The next sentence follows.\n"
    )
    units = _units(text)
    assert (
        "but *advisory only*: it records a counterfactual and never steers "
        "execution, routing, or money)."
    ) in units
    assert not any(u.startswith("only*:") for u in units)
