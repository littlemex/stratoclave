"""L7 (docs/design/limits.md (C14)): the README's budget rows say what is on
by default, in the unit it is enforced in.

L7's Why: "The README currently implies the dollar cap is the default
behaviour; it is opt-in" — describing the state BEFORE this PR. After L3
lands, every tenant created through the ordinary route gets a dollar pool at
creation, so the pool is no longer opt-in, and the README's existing sentence
saying otherwise becomes false.

README.md, today, states (in the "Dollar pool budgets, priced per model" bullet):

    "A tenant with no pool row keeps the token-only behaviour unchanged
    (pools are opt-in per tenant/period)."

That literal sentence is the claim this test pins as false-and-must-change:
after L3, a tenant created through the ordinary route always HAS a pool row,
so "pools are opt-in per tenant/period" is no longer an accurate description
of the default. This test fails today because the sentence is present.
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
README = ROOT / "README.md"

_STALE_CLAIM = "pools are opt-in per tenant/period"


def test_readme_no_longer_claims_pools_are_opt_in():
    text = README.read_text()
    assert _STALE_CLAIM not in text, (
        f"README.md still says {_STALE_CLAIM!r}, which is false once every "
        "tenant gets a default dollar pool at creation (L3)"
    )
