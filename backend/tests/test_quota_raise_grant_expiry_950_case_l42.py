"""F4 / R42 — the $950 case's regression test moved to F2; F4 anchors it.

SEAM CORRECTION B3 / S5 (`CONTRACT-F4-claims (F4's contract document)` "Seam amendments" / `SEAMS (the integration owner's seam-review document)`)

An earlier version of this file OWNED the $950 regression test directly: it
guessed a grant API shape (`TenantBudgetsRepository.grant_pool` /
`.expire_grant`, carrying `pool_granted_microusd`) and asserted the full
invariant against it. `SEAMS (the integration owner's seam-review document)` S5 names why that ownership was wrong: "F4
holds the `$950` case... F2 is where the setter acquires grant-aware
semantics. A guard arriving two parts after the defect becomes possible
cannot prevent it — F2 could ship the bug and F3 could build surfaces over
it, and the test would document a regression rather than block one."

`CONTRACT-F4-claims (F4's contract document)` amendment B3 states the consequence for this part
plainly: "the `$950` regression test moves to F2 ... F4 keeps the final
fixture, the remeasurement, the evidence metadata and the anchoring, and may
anchor both of the above." So F4's remaining role is to ANCHOR the case —
confirm a regression test for it exists, written by the part that can
actually block the regression — not to re-author the behavioural assertions
against a guessed API a second time.

WHAT THIS TEST CHECKS

That some test function in this repository's test suite names the $950 /
grant-expiry regression (loosely matched: "950", or "grant" together with
"expir*" in either order — deliberately not F4's own naming convention,
since F4 does not own F2's test file or function names).

WHY THIS FAILS TODAY

No test anywhere in the repository names this regression yet — F2 has not
landed it. Per S5, F2 must write it BEFORE the setter's grant-aware
semantics change, so that it blocks the regression rather than documents one
that already shipped; F4 cannot substitute for that timing by writing its
own copy here, which is exactly the ownership this correction removes.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND_TESTS = ROOT / "backend" / "tests"

#: A test function name that plausibly covers the $950 / grant-expiry case:
#: "950" anywhere in the name, or "grant" and "expir*" together in either
#: order. Deliberately loose — F4 does not own F2's naming convention, only
#: that SOME such test exists, written by F2.
REGRESSION_NAME = re.compile(
    r"def\s+(test_[a-z0-9_]*"
    r"(?:950|grant[a-z0-9_]*expir|expir[a-z0-9_]*grant)"
    r"[a-z0-9_]*)\s*\(",
    re.IGNORECASE,
)

#: This file itself, excluded from its own search — otherwise a future
#: accidental re-add of behavioural assertions here would let this test pass
#: by matching itself instead of a real F2 guard.
SELF = Path(__file__).resolve()


def test_the_950_case_has_a_named_regression_test_owned_by_f2():
    """B3 / S5: the regression test for "an operator asking for a figure
    while a grant is live must not lose the granted amount when it expires"
    belongs to F2 — the part where the setter acquires grant-aware semantics
    — written BEFORE that setter changes, so it can block the regression
    rather than merely document one that already shipped. F4 anchors the
    case rather than authoring it a second time."""
    found: list[str] = []
    for path in BACKEND_TESTS.rglob("test_*.py"):
        if path.resolve() == SELF:
            continue
        text = path.read_text(errors="replace")
        for match in REGRESSION_NAME.finditer(text):
            found.append(f"{path.relative_to(ROOT)}::{match.group(1)}")
    assert found, (
        "no test in backend/tests/ names the $950 / grant-expiry regression "
        "(a test_* function naming '950', or 'grant' together with "
        "'expir*'). Per CONTRACT-F4-claims (F4's contract document) amendment B3 and SEAMS (the integration owner's seam-review document) S5, "
        "this test belongs to F2, written before the setter acquires "
        "grant-aware semantics — F4 anchors it once it exists rather than "
        "authoring it here a second time."
    )
