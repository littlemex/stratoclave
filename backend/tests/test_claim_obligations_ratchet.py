"""C3/C2/B8 — a claim has an identity, and identities do not quietly disappear.

WHAT DEFECT THIS CLOSES

`test_claims_are_anchored.py` enforces "every guarantee-shaped sentence found today
is registered." That is satisfiable by deleting the sentence, rephrasing it below the
detector, shortening it under the minimum length, moving it into a table or a code
fence, or narrowing the lexicon — none of which is a repayment, all of which are a
loss of detectability. A metric computed only over the CURRENT document text cannot
tell "the promise was kept" apart from "the promise stopped being visible."

The fix is a second, independent record: `contracts/claims/snapshot.json`, a standing
set of claim ids. A claim id may leave that set only through one of five typed
dispositions (`replaced-by`, `moved-to-clause`, `document-removed`, `retracted-false`,
`reworded-to`), each of which this file verifies resolves to something real. The
snapshot is append-mostly by construction: growing it (a new claim appears and is
added deliberately) is normal; shrinking it requires a disposition that this file
checks, not just an editor deleting a line.

The same record watches the DETECTOR, because narrowing it is the other way to make a
guarantee stop being visible without making it stop being made. Staleness catches part
of this already: drop a covered file, or a lexicon term that some registered sentence
matches on and nothing else does, and that sentence's registry entry goes stale. It
does not catch the rest — a term that is redundant over today's corpus can be deleted
in silence, and the loss shows up only when someone later writes the sentence that
term alone would have caught. Measured on the corpus at the time this was written, 23
of the 33 guarantee terms were protected by staleness and 10 were not. So
`snapshot.json` carries a standing set of the declared lexicons, covered files and
protected subjects too, and an entry leaves it the same way a claim does.

WHAT IT DOES NOT DO

It cannot verify that a `disposition`'s prose reason is honest, or that a
`replaced-by`/`moved-to-clause` successor actually carries the retired claim forward
rather than merely existing. That is a human reading of both endpoints, named as a
permanent obligation in `docs/design/CONTRACTS.md` (C7.4). What this file verifies is
narrower and mechanical: the relationship a disposition asserts points at something
that exists, so an empty or fabricated disposition fails rather than passing silently.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from tests import test_claims_are_anchored as lint


SNAPSHOT = lint.CLAIMS_DIR / "snapshot.json"

#: The five, and only five, ways a claim id may leave the live `claims` set. Anything
#: else is a disposition invented on the spot, which is exactly the free-form "reason"
#: B8 replaced.
DISPOSITIONS = {"replaced-by", "moved-to-clause", "document-removed", "retracted-false",
                "reworded-to"}


def _snapshot() -> dict:
    return json.loads(SNAPSHOT.read_text())


def _registry_ids() -> set[str]:
    return {c["id"] for c in lint._registry()["claims"]}


def _open_items_text() -> str:
    return lint.CONTRACTS.read_text().split(
        "## Open items, named rather than implied", 1)[-1]


def _target_resolves(disposition: str, target: str) -> bool:
    """Whether a disposition's `target` points at something that actually exists.

    Each disposition names a different kind of object, so each is checked against a
    different source of truth — the registry, the clause list, the filesystem, or the
    contract's own open items — rather than one generic "is this a nonempty string".
    """
    target = str(target).strip()
    if not target:
        return False
    if disposition in ("replaced-by", "reworded-to"):
        # The successor has to still be a live, anchored claim — not another retired
        # id, which would just move the hole one hop over instead of closing it.
        return target in _registry_ids()
    if disposition == "moved-to-clause":
        return target in lint._clause_ids()
    if disposition == "document-removed":
        # The whole point of this disposition is that the sentence's HOME is gone, not
        # merely that the sentence is no longer a lint candidate. A path that still
        # exists means the claim was uncovered, not removed.
        return not (lint.ROOT / target).exists()
    if disposition == "retracted-false":
        # A retraction stays permanently visible rather than vanishing into a tidy
        # "closed" state, so its target has to name something a reader can find in the
        # contract's own open-items ledger: an open item, or an evidence row.
        return target in _open_items_text() or target in (
            lint.ROOT / "docs" / "EVIDENCE.md").read_text()
    return False


# --------------------------------------------------------------------- the checks


def test_the_ratchet_is_not_vacuous():
    """A snapshot with nothing in it, or a registry with nothing in it, passes every
    other test in this file for free — which is the ratchet becoming decoration rather
    than a check. Fail loudly instead of passing quietly on an empty world."""
    snap = _snapshot()
    assert snap["claims"] or snap["retired"], (
        "contracts/claims/snapshot.json has no claims and no retirements: "
        "the ratchet is not watching anything")
    assert _registry_ids(), (
        "contracts/claims/anchored.json is empty: nothing for the ratchet to compare "
        "the snapshot against")


def test_no_id_is_both_live_and_retired():
    """An id in both halves of the snapshot is a claim that is simultaneously still
    owed and marked as discharged, which is the state this file exists to make
    impossible to leave unnoticed."""
    snap = _snapshot()
    both = set(snap["claims"]) & set(snap["retired"])
    assert not both, f"ids listed as both live and retired: {sorted(both)}"


def test_every_snapshot_claim_is_registered_or_retired():
    """The heart of the ratchet: an id that leaves `claims` without a matching entry
    in `retired` is a claim that disappeared with nobody accounting for where it went
    — which is exactly the hole a text-only lint cannot see, because from the text's
    point of view there is nothing left to look at."""
    snap = _snapshot()
    registered = _registry_ids()
    retired = snap["retired"]
    missing = [
        cid for cid in snap["claims"]
        if cid not in registered and cid not in retired
    ]
    assert not missing, (
        f"{len(missing)} claim id(s) are neither in contracts/claims/anchored.json "
        f"nor listed under snapshot.json's `retired`, so they left the inventory with "
        f"no disposition: {missing[:20]}"
    )


def test_every_retirement_has_a_typed_disposition_that_resolves():
    """B8: a retired claim names one of five dispositions, and each names something
    that verifiably exists — a live successor claim, a live clause, a genuinely
    deleted file, or a standing open item. An author-invented reason ('cleanup',
    'wording changed') is exactly what this test refuses to accept in its place."""
    snap = _snapshot()
    problems: list[str] = []
    for cid, entry in snap["retired"].items():
        disposition = str(entry.get("disposition", ""))
        target = entry.get("target", "")
        if disposition not in DISPOSITIONS:
            problems.append(
                f"{cid}: disposition {disposition!r} is not one of {sorted(DISPOSITIONS)}")
            continue
        if not str(entry.get("why", "")).strip():
            problems.append(f"{cid}: retired with no `why`")
        if not _target_resolves(disposition, target):
            problems.append(
                f"{cid}: disposition {disposition!r} names target {target!r}, which "
                f"does not resolve")
    assert not problems, "\n  ".join([""] + problems)


def test_every_registered_claim_is_in_the_snapshot():
    """A new candidate entering `contracts/claims/anchored.json` has to be added to the
    snapshot in the same change, deliberately — never picked up implicitly by a test
    that only looks at what is missing. An id present in the registry but absent from
    the snapshot is a claim nobody put under the ratchet's watch."""
    snap = _snapshot()
    tracked = set(snap["claims"]) | set(snap["retired"])
    untracked = sorted(_registry_ids() - tracked)
    assert not untracked, (
        f"{len(untracked)} registered claim id(s) are not in snapshot.json's `claims` "
        f"(or `retired`), so they were added to the registry without being added to "
        f"the ratchet: {untracked[:20]}"
    )


#: The sets in `contracts/claims/config.json` whose entries are under the ratchet. Each
#: is a way the lint can see less than it saw yesterday.
WATCHED_DETECTOR_SETS = ("guarantee_terms", "boundary_terms", "covered_documents",
                         "protected_subjects")

#: How a detector entry may leave. Deliberately not the claim dispositions: a lexicon
#: term is not replaced by a claim and cannot be moved to a clause. `subsumed-by` names
#: a surviving entry that still catches everything this one caught, which is checkable
#: over the registered corpus; `retracted-false` names an open item, the same as for a
#: claim, for an entry that was wrong to declare in the first place.
DETECTOR_DISPOSITIONS = {"subsumed-by", "retracted-false"}


def _detector() -> dict:
    snap = _snapshot()
    assert "detector" in snap, (
        "snapshot.json has no `detector` section, so nothing is watching the lexicons, "
        "the covered file list or the protected subjects")
    return snap["detector"]


def _matches(pattern: str, kind: str, text: str) -> bool:
    """Whether one declared entry applies to one sentence, by the same rule the lint
    uses for that kind — a path is compared as a path, a lexicon term as a regex."""
    import re
    if kind == "covered_documents":
        return pattern == text
    return re.search(pattern, text, re.IGNORECASE) is not None


def test_every_detector_entry_is_declared_or_retired():
    """A term, a covered file or a protected subject may not leave the config silently.

    This is the half staleness does not cover. A guarantee term that no registered
    sentence depends on ALONE can be deleted today without a single test noticing,
    because nothing is currently stale without it — and the sentence it would have
    caught has not been written yet. That sentence is the whole point of the term.
    """
    detector = _detector()
    cfg = lint.config()
    problems: list[str] = []
    for kind in WATCHED_DETECTOR_SETS:
        declared = set(map(str, cfg[kind]))
        for entry in detector["standing"].get(kind, []):
            if str(entry) in declared:
                continue
            retirement = detector["retired"].get(str(entry))
            if retirement is None:
                problems.append(
                    f"{kind}: {entry!r} was in the standing set and is no longer "
                    f"declared in config.json, with no disposition recorded")
                continue
            if str(retirement.get("kind", "")) != kind:
                problems.append(f"{kind}: {entry!r} is retired under kind "
                                f"{retirement.get('kind')!r}")
    assert not problems, "\n  ".join([""] + problems)


def test_every_declared_detector_entry_is_in_the_standing_set():
    """The mirror: an entry added to the config has to be added to the standing set in
    the same change, so the set stays the record of what the detector is rather than a
    stale copy that quietly stops being compared against anything."""
    detector = _detector()
    cfg = lint.config()
    problems: list[str] = []
    for kind in WATCHED_DETECTOR_SETS:
        standing = set(map(str, detector["standing"].get(kind, [])))
        for entry in map(str, cfg[kind]):
            if entry not in standing:
                problems.append(f"{kind}: {entry!r} is declared in config.json but not "
                                f"in snapshot.json's standing set")
    assert not problems, "\n  ".join([""] + problems)


def test_every_detector_retirement_resolves():
    """`subsumed-by` is checkable and is checked: the named survivor has to still be
    declared, and it has to match every registered sentence the retired entry matched.
    An entry removed because "nothing uses it" fails here unless something else covers
    the sentences it did cover — which is the claim `subsumed-by` makes.

    Its limit is stated rather than papered over: coverage is compared over the
    sentences in the registry today, so `subsumed-by` means "catches everything this
    caught, here, now", not "catches every string this pattern could ever match".
    """
    detector = _detector()
    cfg = lint.config()
    registered = [str(c["claim"]) for c in lint._registry()["claims"]]
    problems: list[str] = []
    for entry, retirement in detector["retired"].items():
        disposition = str(retirement.get("disposition", ""))
        kind = str(retirement.get("kind", ""))
        target = str(retirement.get("target", "")).strip()
        if disposition not in DETECTOR_DISPOSITIONS:
            problems.append(f"{entry!r}: disposition {disposition!r} is not one of "
                            f"{sorted(DETECTOR_DISPOSITIONS)}")
            continue
        if not str(retirement.get("why", "")).strip():
            problems.append(f"{entry!r}: retired with no `why`")
        if disposition == "retracted-false":
            if target not in _open_items_text():
                problems.append(f"{entry!r}: retracted-false names {target!r}, which is "
                                f"not in the contract's open items")
            continue
        if target not in set(map(str, cfg.get(kind, []))):
            problems.append(f"{entry!r}: subsumed-by names {target!r}, which is not "
                            f"declared under {kind!r}")
            continue
        lost = [s for s in registered
                if _matches(entry, kind, s) and not _matches(target, kind, s)]
        if lost:
            problems.append(
                f"{entry!r}: subsumed-by {target!r} does not catch "
                f"{len(lost)} sentence(s) it caught, e.g. {lost[0][:120]!r}")
    assert not problems, "\n  ".join([""] + problems)


def test_the_detector_ratchet_is_not_vacuous():
    """The same guard the claim ratchet has. An empty standing set passes all three
    tests above for free, and a standing set that is merely a copy of the config made
    at read time would too."""
    detector = _detector()
    for kind in WATCHED_DETECTOR_SETS:
        assert detector["standing"].get(kind), (
            f"snapshot.json's standing set for {kind!r} is empty, so nothing is watching "
            f"it")
    # A survivor that catches nothing cannot subsume anything: prove the coverage check
    # has teeth by asking it about a pattern that matches no registered sentence.
    registered = [str(c["claim"]) for c in lint._registry()["claims"]]
    assert not any(_matches(r"\bzzzz-not-a-real-term\b", "guarantee_terms", s)
                   for s in registered)
    assert any(_matches(r"\bnever\b", "guarantee_terms", s) for s in registered), (
        "the matcher used by the subsumption check finds nothing in the registry, so it "
        "would accept any subsumption claim")


def test_unresolved_claim_obligations():
    """Reports the number C3 defines: live unanchored claims, plus retired-but-
    undischarged claims. This number cannot be lowered by deleting a sentence — that
    is the entire reason the snapshot exists instead of a count over document text.
    Deleting a sentence deletes a lint candidate, not a snapshot entry; the id stays in
    `claims` until it is moved to `retired` with a disposition that resolves, and a
    disposition that does not resolve is counted right back in below. The only way to
    lower this number is to register the claim, or to retire it honestly.
    """
    snap = _snapshot()
    registered = _registry_ids()

    live_unanchored = sorted(
        cid for cid in snap["claims"]
        if cid not in registered
    )
    retired_undischarged = sorted(
        cid for cid, entry in snap["retired"].items()
        if not _target_resolves(str(entry.get("disposition", "")), entry.get("target", ""))
    )

    total = len(live_unanchored) + len(retired_undischarged)
    print(f"unresolved claim obligations: {total}")
    for cid in live_unanchored:
        print(f"  live, unanchored: {cid}")
    for cid in retired_undischarged:
        print(f"  retired, undischarged: {cid}")

    # This test reports rather than gates: gating on a moving number is what B1 (the
    # ceiling) was withdrawn for. The gate is the two tests above, which already fail
    # if either half of this total is nonzero; this assertion only guards against the
    # count and the detail lists disagreeing with each other.
    assert total == len(live_unanchored) + len(retired_undischarged)
