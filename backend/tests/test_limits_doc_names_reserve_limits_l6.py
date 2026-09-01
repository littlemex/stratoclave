"""L6 (docs/design/limits.md (C14)): `docs/design/limits.md` (new) documents
the four ceilings — unit, scope, on-by-default, and what each protects — and
the sentence that tokens are a per-request bound and a fairness device, never
a money ceiling.

The L6 row's "Verified by" column names the mechanical half of this precisely:
"The claim lint anchors it, and a test asserts every ceiling named in
`reserve_limits.RESERVE_LIMITS` appears in the document." That is the one part
of L6 stateable as a test from the interface alone (the claim-lint anchoring
itself is `contracts/claims/*` + `docs/design/CONTRACTS.md` content, which is
production documentation, not a test file, and out of this file's scope to
write).

`docs/design/limits.md` does not exist at all today, so every assertion below
fails on `FileNotFoundError` / `AssertionError` until it is written.
"""
from __future__ import annotations

import pathlib

import pytest

from mvp.reserve_limits import RESERVE_LIMITS

ROOT = pathlib.Path(__file__).resolve().parents[2]
LIMITS_DOC = ROOT / "docs" / "design" / "limits.md"


def test_limits_doc_exists():
    assert LIMITS_DOC.is_file(), f"{LIMITS_DOC} does not exist yet"


def test_every_reserve_limit_kind_is_named_in_the_doc():
    if not LIMITS_DOC.is_file():
        pytest.fail(f"{LIMITS_DOC} does not exist yet")
    text = LIMITS_DOC.read_text()
    missing = [k.name for k in RESERVE_LIMITS if k.name not in text]
    assert not missing, (
        f"docs/design/limits.md is missing these RESERVE_LIMITS names: {missing}"
    )


def test_doc_states_the_token_quota_is_a_fairness_device_not_a_money_ceiling():
    """L6's Why: 'the sentence that tokens are a per-request bound and a
    fairness device, never a money ceiling' — the one normative sentence the
    contract quotes almost verbatim. Checked loosely (both key phrases present
    anywhere in the doc) since the exact wording is the author's to choose."""
    if not LIMITS_DOC.is_file():
        pytest.fail(f"{LIMITS_DOC} does not exist yet")
    text = LIMITS_DOC.read_text().lower()
    assert "fairness" in text, "the fairness-device framing is not in the document"
    assert "money" in text, "the doc never contrasts a ceiling against money"
