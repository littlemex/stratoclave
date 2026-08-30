"""C10.5 — the 1.0 promise, enforced instead of merely written down.

WHAT DEFECT THIS CLOSES

`CHANGELOG.md` says the clause levels in `docs/design/CONTRACTS.md` are the compatibility
surface and that a level is not lowered in a minor or patch release. That is the most
load-bearing sentence the 1.0 line contains: it is what a reader pins a tag ON.

It was also invisible. The claim lint's guarantee lexicon is aimed at runtime behaviour —
admission, charges, identity — so a promise about the PROJECT'S process ("a level is not
lowered") matches nothing, is not a candidate, and is not registered. The lint went green
over it and that green said nothing. The same shape as the README sentence about refunding
unused credit, which was outside the lexicon for months while sitting on the money path.

Widening the lexicon to catch process promises would drag in a lot of prose that has no
honest anchor kind. The better answer for this one is that the promise is mechanically
checkable, so it gets checked: the levels are recorded, a level may only weaken when the
released major version has incremented, and this file fails otherwise.

WHAT IS AND IS NOT CHECKED

Checked: that no clause's level weakens without a major bump, that a clause does not
vanish without one, and that the recorded set has not silently drifted out of sync with
the document.

Not checked: whether a level is CORRECT. That a clause claiming **E** really does have a
test that fails when it stops holding is the first of the permanent human obligations in
`CONTRACTS.md`, and no ratchet reaches it. This file only refuses the quiet downgrade —
weakening the claim rather than fixing the code, between releases, with nobody's attention
on the diff.
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

from tests import test_claims_are_anchored as lint


SNAPSHOT = lint.CLAIMS_DIR / "snapshot.json"
CHANGELOG = lint.ROOT / "CHANGELOG.md"

#: Ordered strongest to weakest. The order IS the ratchet: an index that rises is a
#: weakening. `B` and `N` are both weaker than `E`, and they are not comparable with each
#: other — a clause moving between them is a change of kind, not of strength — so they
#: share a rank and such a move is reported for a human rather than passed or failed.
_RANK = {"P": 0, "E": 1, "B": 2, "N": 2}


def _released_major() -> int:
    """The major version of the most recent RELEASED version, read from the changelog.

    From the changelog rather than from a git tag on purpose: every check in this
    repository runs from a clean checkout under plain pytest, with no git and no CI-only
    step. An `[Unreleased]` heading is skipped — work in progress is not a release, and
    treating it as one would let a weakening ride in under a major bump nobody has cut.
    """
    for line in CHANGELOG.read_text().splitlines():
        m = re.match(r"^## \[(\d+)\.(\d+)\.(\d+)\]", line.strip())
        if m:
            return int(m.group(1))
    pytest.fail("CHANGELOG.md has no released version heading, so this ratchet has no "
                "baseline to compare a weakening against")


def _declared_levels() -> dict[str, str]:
    """`{clause id: level letter}` from the contract's own rows.

    The level cell is prose, not an enum: it says things like "E, with a **P** subset",
    "E in the default deployment; **B** if an operator turns the gate off", and "**B** —
    and NOT in the default deployment". A clause that holds at two levels is recorded at
    the WEAKEST of them, because that is what a reader can rely on without reading the
    condition — and because recording the strongest would let a clause weaken by adding a
    caveat while the recorded letter stayed put.
    """
    rows = lint._clause_rows()
    out: dict[str, str] = {}
    for cid, row in rows.items():
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        level_cell = cells[1] if len(cells) > 1 else ""
        found = {letter for letter in ("P", "E", "B", "N")
                 if re.search(rf"(?<![A-Za-z]){letter}(?![A-Za-z])", level_cell)}
        if not found:
            out[cid] = "?"
            continue
        out[cid] = max(found, key=lambda letter: _RANK[letter])
    return out


def _recorded_levels() -> dict[str, str]:
    return dict(json.loads(SNAPSHOT.read_text())["detector"]["standing"].get(
        "clause_levels", {}))


# ---------------------------------------------------------------------- checks


def test_the_level_ratchet_is_not_vacuous():
    """An empty record passes every check below for free, and a record that is merely
    re-read from the document at test time is not a record at all."""
    recorded = _recorded_levels()
    declared = _declared_levels()
    assert recorded, (
        "snapshot.json has no `clause_levels`, so nothing is watching the compatibility "
        "surface the changelog promises")
    assert len(recorded) >= 60, (
        f"only {len(recorded)} clause levels recorded; the contract has {len(declared)}, "
        f"so the record has drifted into covering a fraction of it")
    assert "?" not in set(declared.values()), (
        f"a clause row has no parseable level: "
        f"{sorted(cid for cid, level in declared.items() if level == '?')}")


def test_every_clause_level_is_recorded():
    """A clause added without recording its level is a clause outside the promise. The
    record has to grow deliberately, in the same change, or the surface quietly shrinks to
    whatever happened to be written down first."""
    missing = sorted(set(_declared_levels()) - set(_recorded_levels()))
    assert not missing, (
        f"these clauses have no recorded level, so a later weakening of them would not "
        f"be caught: {missing}")


def test_no_clause_level_weakened_without_a_major_bump():
    """C10.5. The promise itself.

    A level that has moved down since it was recorded is a claim the project weakened. It
    is allowed — a clause discovered to be untrue at **E** SHOULD drop to **B** rather
    than keep lying — but it is a breaking change to what a reader pinned, so it costs a
    major version. Doing it inside a patch release is the quiet downgrade this exists to
    refuse: cheaper than fixing the code, and invisible in a diff nobody reads.
    """
    recorded = _recorded_levels()
    declared = _declared_levels()
    major = _released_major()

    weakened: list[str] = []
    for cid, was in recorded.items():
        now = declared.get(cid)
        if now is None:
            weakened.append(f"{cid}: was {was}, and the clause is gone")
            continue
        if now == was:
            continue
        if _RANK[now] > _RANK[was]:
            weakened.append(f"{cid}: {was} -> {now}")

    if not weakened:
        return
    # The escape hatch is a major bump, and it is the only one. Recording the new levels
    # is then part of cutting that release.
    baseline = int(json.loads(SNAPSHOT.read_text())["detector"].get(
        "clause_levels_major", major))
    assert major > baseline, (
        "these clause levels weakened since they were recorded, which is a breaking "
        "change to the compatibility surface CHANGELOG.md promises:\n  "
        + "\n  ".join(weakened)
        + f"\n\nThe released major version is still {major}. Either restore the level by "
        f"fixing what made it untrue, or cut a major release and re-record the levels "
        f"together with `clause_levels_major`."
    )


def test_a_move_between_b_and_n_is_reported_rather_than_judged():
    """`B` and `N` are not comparable: "true inside a stated configuration" and
    "deliberately not guaranteed" are different kinds of claim, not different strengths.
    A clause moving between them is a change a human should read, so it is printed rather
    than silently passed — and rather than failed, which would make the honest act of
    reclassifying one cost a major version for no reader benefit."""
    recorded = _recorded_levels()
    declared = _declared_levels()
    moves = [
        f"{cid}: {was} -> {declared[cid]}"
        for cid, was in recorded.items()
        if cid in declared and was != declared[cid]
        and {was, declared[cid]} == {"B", "N"}
    ]
    for move in moves:
        print(f"clause level changed kind (read both rows): {move}")
    assert True


def test_a_strengthening_is_allowed_and_wants_re_recording():
    """The other direction. Strengthening is always fine, but leaving the old weaker
    letter in the record means the ratchet would later accept a slide back to it for free
    — a clause that went B to E and back to B would never be seen weakening."""
    recorded = _recorded_levels()
    declared = _declared_levels()
    stale = [
        f"{cid}: recorded {was}, now {declared[cid]}"
        for cid, was in recorded.items()
        if cid in declared and _RANK.get(declared[cid], 9) < _RANK[was]
    ]
    assert not stale, (
        "these clauses are now STRONGER than the record, which is good — but re-record "
        "them, or the ratchet will let them slide back to the recorded level without "
        "noticing:\n  " + "\n  ".join(stale))
