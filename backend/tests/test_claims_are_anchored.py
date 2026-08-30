"""C10 — every guarantee-shaped sentence in the public documents has an anchor.

The contract's opening rule is that a clause with no test is a statement about one
commit rather than about the project. C10 — "every guarantee in the public documents
is true of the shipped code, or states the boundary at which its evidence stops" —
was the one clause that had no test, so it was exactly that: a statement about the
commit where someone last read the README. Both reviewers of the contract audit said
so independently, and both proposed this lint.

WHAT IT ENFORCES

A sentence in a covered document that uses guarantee vocabulary must be registered
in `contracts/claims/anchored.json` with an anchor saying WHY it is allowed to say
that. Four anchor kinds, and the lint checks each differently:

  contract:<clause>   The clause exists in docs/design/CONTRACTS.md, which carries
                      its guarantee level and its enforcing test. The strongest
                      anchor: the sentence is as true as that clause is.
  evidence:<text>     The text appears in docs/EVIDENCE.md, which is where a claim's
                      evidence — and the point it runs out — is recorded.
  boundary:<why>      The sentence carries its own limit, and the lint requires a
                      boundary word IN the sentence. For claims whose honesty is in
                      the qualification rather than in a clause.
  descriptive:<why>   The vocabulary matched but the sentence makes no claim about
                      this gateway — a competitor's feature list, a definition, a
                      quoted failure. Requires a reason, so "descriptive" cannot
                      become a silent escape hatch.
  debt:<clause>       The sentence is TRUE AS QUALIFIED, and the named clause is what
                      would make it unconditional. The clause must exist and must be
                      listed under "Open items" in CONTRACTS.md, so a softened
                      sentence and the work that would strengthen it are the same
                      list. This anchor exists because removing a false claim by
                      weakening the wording is how a project talks itself out of its
                      own moat: the weakened sentence has to stay visible as a debt,
                      not settle in as the final answer.

Adding a guarantee sentence therefore fails the build until its author says what
makes it true. Editing one fails too, because the registry matches on the text.

WHAT IT DOES NOT DO

It cannot tell whether a sentence is true — only whether someone was made to point
at the reason. That is the honest limit of a lint, and it is the difference between
"we are careful" and "carelessness fails the build".
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "contracts" / "claims" / "anchored.json"

#: The documents a reader consults to decide whether to trust this gateway. These
#: are the ones whose sentences must be anchored. EVIDENCE.md is deliberately NOT
#: covered: it is the anchor document, the place where a claim's evidence and its
#: boundary are recorded, so requiring it to anchor to itself is circular.
COVERED = (
    "README.md",
    "docs/SCOPE.md",
    "docs/design/hard-ceiling.md",
)

#: Vocabulary that turns a sentence into a promise. Deliberately broad: a false
#: positive costs one registry line with `descriptive:`, a false negative is an
#: unexamined guarantee.
GUARANTEE = re.compile(
    r"\b(cannot|can never|never|always(?!-on)|guarantee[sd]?|impossible|"
    # A safety property is often stated without a modal verb at all: "consumed
    # once", "fail-closed", "at most once". The vouch-replay guarantee — a signed
    # GetCallerIdentity accepted exactly once, and a 401 rather than trust when the
    # nonce store is unreachable — went entirely unregistered under the earlier
    # lexicon, which is the shape of miss that matters most: a security claim with
    # no anchor reads exactly like one with an anchor.
    r"exactly once|at most once|only once|consumed once|single-use|"
    r"fail-closed|fails closed|fail closed|"
    r"no request|no client|proven|proved|proof|machine-checked|formally|"
    r"ensures?|enforced|bypass-proof|crash-safe|reproducible|provable|verified|"
    # A sentence naming Z3, an invariant, or Hypothesis is claiming a proof even
    # without a modal verb. "a ledger with a Z3-checked no-double-post invariant"
    # went unanchored under the earlier lexicon: rewriting "Z3-proven" into
    # "Z3-checked invariant" had removed every word the lint was looking for while
    # keeping the whole of the claim.
    r"z3|invariants?|hypothesis|"
    r"hard at admission)\b",
    re.IGNORECASE,
)

#: Words that make a claim carry its own limit. A `boundary:` anchor requires one.
BOUNDARY = re.compile(
    r"(?:\bunless\b|\bexcept|\bexcluding\b|\bonly\b|\bboundary\b|\bnot\b|"
    r"\bneither\b|by default|defaults? off|can still|remaining work|\bbounded\b|"
    r"\bdelegated\b|explicitly out|out of scope|\bwithout\b|\bstates?\b|"
    r"no longer|\buntil\b|\bassum|\bwithin\b|\bscope[ds]\b|so far as|"
    r"to the extent|\bcaveat\b|but the|\bhowever\b|we do not|unverified|"
    r"\babsent\b|open item|per dimension|once gating|as qualified|\bwithdrawn\b|"
    r"below target|in production|for the duration of|\badvisory\b|conditional|"
    r"unchecked|rather than|money model|over the model|modelled)",
    re.IGNORECASE,
)

#: This design document scopes a statement by naming the mode it applies to, which is
#: a limit as real as the word "unless". A `boundary:` anchor is satisfied by either.
MODE = re.compile(r"`(accounting|measured|shadow|enforced|strict|calibrated)`")

MIN_LEN = 40

#: A sentence qualified BECAUSE the unconditional version is not true yet is a debt,
#: not a boundary. Without this, `boundary:` absorbs every unbuilt thing and C10.2 —
#: "a weakened sentence and the work that would strengthen it are the same list" — is
#: satisfied vacuously. Found by the review round that asked whether the documents had
#: got weaker than they needed to be: one debt anchor in a registry of 160.
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

#: The names this project goes by. `descriptive:` means "not a claim about this
#: gateway", so a sentence that NAMES the gateway cannot be filed that way — without
#: this check, `descriptive:` is the escape hatch the whole registry is meant not to
#: have. (The check comes from the shared checker in `contracts/`, whose five-anchor
#: model this lint follows; it is vendored here because a public repository cannot
#: depend on a tool outside it.)
SUBJECT_NAMES = ("stratoclave", "this gateway", "the gateway", "this project")


def _units(text: str) -> list[str]:
    """Whole sentences, and table cells as sentences of their own.

    Prose here is hard-wrapped, so a naive per-line split yields fragments — and a
    registry keyed on fragments would fail the build every time someone re-wrapped a
    paragraph, which teaches authors to weaken the lint. Wrapped lines are joined
    into their paragraph first and only then split on sentence ends.

    A table cell is its own unit because a verdict word in a comparison table is a
    claim by itself: that is how "Crash-safe budget accounting: Yes" came to be
    contradicted three sentences into its own cell.
    """
    out: list[str] = []
    paragraph: list[str] = []
    in_fence = False

    def flush() -> None:
        if paragraph:
            joined = " ".join(paragraph)
            out.extend(re.split(r"(?<=[.!?])\s+", joined))
            paragraph.clear()

    for line in text.split("\n"):
        stripped = line.strip()
        # A fenced block is code, not a sentence. Its lines used to be joined into
        # the surrounding paragraph, which turned a list of `make` targets into a
        # guarantee-shaped "sentence" naming the gateway — a registry entry whose
        # only content was that someone had written a shell comment.
        if stripped.startswith("```"):
            flush()
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if stripped.startswith("|"):
            flush()
            out += [c.strip() for c in line.split("|") if c.strip()]
            continue
        if not stripped or stripped.startswith(("#", "```", "---", ">")):
            flush()
            if stripped:
                out.append(stripped)
            continue
        # A list item starts a new unit; a continuation line extends the current one.
        if re.match(r"^([-*+]|\d+\.)\s", stripped):
            flush()
        paragraph.append(stripped)
    flush()
    return [u.strip() for u in out if u.strip()]


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def candidates() -> dict[str, list[str]]:
    """{relative path: [claim sentence, ...]} over the covered documents."""
    found: dict[str, list[str]] = {}
    for rel in COVERED:
        text = (ROOT / rel).read_text()
        hits = [
            _normalize(u) for u in _units(text)
            if GUARANTEE.search(u) and len(u) >= MIN_LEN
        ]
        found[rel] = hits
    return found


def _registry() -> dict:
    if not REGISTRY.exists():
        return {"claims": []}
    return json.loads(REGISTRY.read_text())


def _clause_ids() -> set[str]:
    text = (ROOT / "docs" / "design" / "CONTRACTS.md").read_text()
    return set(re.findall(r"\*\*(C\d+(?:\.\d+[a-c]?)?)\*\*", text))


def test_every_claim_sentence_is_registered():
    """A guarantee-shaped sentence with no registry entry fails the build.

    The fix is never to soften the lint: it is either to anchor the sentence, to
    state its boundary in the sentence, or to delete the claim.
    """
    reg = {_normalize(c["claim"]): c for c in _registry()["claims"]}
    unregistered: list[str] = []
    for rel, claims in candidates().items():
        for claim in claims:
            if claim not in reg:
                unregistered.append(f"{rel}: {claim[:160]}")
    assert not unregistered, (
        f"{len(unregistered)} guarantee sentences have no anchor in "
        f"contracts/claims/anchored.json:\n  " + "\n  ".join(unregistered[:40])
    )


def test_no_registry_entry_is_stale():
    """A registry entry whose sentence no longer exists is a claim someone edited
    without re-examining. Remove the entry in the same change as the edit."""
    live = {c for claims in candidates().values() for c in claims}
    stale = [
        c["claim"][:160] for c in _registry()["claims"]
        if _normalize(c["claim"]) not in live
    ]
    assert not stale, (
        "registry entries no longer present in the documents:\n  " + "\n  ".join(stale)
    )


def test_every_anchor_resolves():
    """An anchor that points at nothing is not an anchor."""
    clause_ids = _clause_ids()
    evidence = (ROOT / "docs" / "EVIDENCE.md").read_text()
    contracts = (ROOT / "docs" / "design" / "CONTRACTS.md").read_text()
    open_items = contracts.split("## Open items, named rather than implied", 1)[-1]
    problems: list[str] = []
    for c in _registry()["claims"]:
        anchor = str(c.get("anchor", ""))
        claim = _normalize(c["claim"])
        kind, _, rest = anchor.partition(":")
        if kind == "contract":
            if rest not in clause_ids:
                problems.append(f"{anchor} is not a clause in CONTRACTS.md — {claim[:80]}")
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
            # Strip inline code spans first: `stratoclave admin tenant pool-budget
            # set` is the name of a command, and a sentence saying where that
            # command's behaviour is documented is not a claim about the gateway.
            # Prose that names the subject outside backticks still cannot be filed
            # as describing someone else.
            lowered = re.sub(r"`[^`]*`", " ", claim).lower()
            named = [n for n in SUBJECT_NAMES if n in lowered]
            if named:
                problems.append(
                    f"descriptive anchor but the sentence names {named[0]!r}, so it is a "
                    f"claim about this gateway — {claim[:100]}")
        elif kind == "debt":
            # A debt must name a real clause AND be on the open-items list, or the
            # qualification quietly becomes the final state. It is NOT required to
            # carry boundary vocabulary: a debt sentence often states the gap
            # outright ("never appears in the ledger at all"), which is the honest
            # shape and would fail a limiter check.
            if rest not in clause_ids:
                problems.append(
                    f"{anchor} is not a clause in CONTRACTS.md — {claim[:80]}")
            elif rest not in open_items:
                problems.append(
                    f"{anchor} is not named under Open items in CONTRACTS.md, so the "
                    f"weakened wording is not tracked as work — {claim[:80]}")
            if not str(c.get("note", "")).strip():
                problems.append(
                    f"debt anchor with no note saying what would close it — {claim[:80]}")
        else:
            problems.append(f"unknown anchor kind {anchor!r} — {claim[:80]}")
        # `descriptive:` and `evidence:` are exempt: a plain status note ("X is not
        # built") and a pointer to the document whose job is separating proven from
        # not-built are not weakened guarantees. A `contract:` or `boundary:` sentence
        # that says the thing is not built IS one.
        if kind in ("contract", "boundary") and NOT_YET.search(claim):
            problems.append(
                f"the sentence says the thing is not built yet, so it is a debt and "
                f"needs `debt:<clause>` naming what would close it — {claim[:110]}")
        if PROOF.search(claim) and not PROOF_BOUNDARY.search(claim):
            problems.append(
                f"names a proof without saying what is proven or where the proof stops "
                f"— {claim[:110]}")
    assert not problems, "\n  ".join([""] + problems)


def test_the_registry_is_not_a_wildcard():
    """Every entry names one sentence, verbatim.

    A pattern or a prefix would let a future edit inherit an anchor it was never
    examined for. What makes that impossible is not a character check — markdown
    emphasis is full of asterisks — but that the entry must appear in its document
    exactly, which `test_no_registry_entry_is_stale` establishes. This test covers
    the remaining shapes: an empty claim, and one too short to identify a sentence.
    """
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
    two an author under time pressure would reach for. If most of the document's
    claims rest on a judgement, the lint has become paperwork. The threshold is not
    a target — it is the point at which this test asks for the claims to be anchored
    to clauses instead.
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
    """The debts are the project's own challenge list, so they are printed rather
    than merely permitted: a reader of the test output sees what would have to be
    built for each qualified sentence to become unconditional."""
    debts = [c for c in _registry()["claims"] if str(c["anchor"]).startswith("debt:")]
    for c in debts:
        clause = str(c["anchor"]).partition(":")[2]
        print(f"debt {clause}: {c['claim'][:110]}")
    # No assertion on the count. A debt is honest; hiding one is not.
    assert all(str(c["anchor"]).partition(":")[2] for c in debts)
