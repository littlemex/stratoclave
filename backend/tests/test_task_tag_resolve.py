"""PR1 (per-user-money-raises, task tags) — the one canonicaliser.

Contract: `change-pipeline/per-user-money-raises/03-impl/HANDOFF-PR1.md`, new
module `backend/mvp/task_tag.py`.

  P1.2 "The tag must be resolved by one pure total function, declared once,
  with its canonical form and its reserved sentinel" — verified by a property
  test (idempotent canonicalisation) and a static check that no module
  outside the declaration normalises a tag.

  P1.3 "A malformed tag must not refuse the request" — this file pins the
  pure function's half of that guarantee: `resolve` never raises, and a
  malformed / over-long / control-character / '#'-bearing tag is dropped to
  the sentinel with source `dropped_grammar` rather than raising. The
  HTTP-level half (the endpoint returns the SAME status as no tag at all) is
  `test_task_tag_never_refuses.py`.

  P1.4 "`unlabelled` must be unusable as an asserted tag" — the reserved
  token, in ANY casing, resolves to `dropped_grammar`, never to `asserted`.

`mvp.task_tag` does not exist at the base commit, so every test below fails
on `ModuleNotFoundError` for that reason — the interface names a module this
worktree does not have, which is the correct "surface absent" failure this
phase is supposed to produce.
"""
from __future__ import annotations

import pathlib

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from mvp.task_tag import (
    GRAMMAR,
    MAX_LEN,
    SENTINEL,
    HDR_TASK_TAG,
    Source,
    canonical,
    resolve,
)
from mvp.observability.context import _ID_GRAMMAR

DEFAULT_SETTINGS = settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TASK_TAG_MODULE = _BACKEND_ROOT / "mvp" / "task_tag.py"


# ---------------------------------------------------------------------------
# P1.2 — sentinel / grammar sanity, and the "declared once" structural checks
# ---------------------------------------------------------------------------

class TestDeclaredOnce:
    def test_sentinel_is_the_literal_string_unlabelled(self):
        assert SENTINEL == "unlabelled"

    def test_max_len_is_64(self):
        assert MAX_LEN == 64

    def test_header_name_is_x_sc_task_tag(self):
        assert HDR_TASK_TAG == "x-sc-task-tag"

    def test_grammar_is_the_correlation_id_grammar_reused_not_copied(self):
        """The interface is explicit: 'GRAMMAR: re.Pattern # the
        correlation-id grammar, reused not copied'. A second `re.compile`
        with an identical-looking pattern string is exactly the drift this
        entry exists to prevent (a future edit to one grammar silently stops
        applying to the other) — so this checks the SAME compiled Pattern
        object, not merely an equal `.pattern` string.
        """
        assert GRAMMAR is _ID_GRAMMAR

    def test_no_other_module_defines_its_own_nfkc_normaliser(self):
        """P1.2's static check, adapted from this suite's existing convention
        for 'this module does not contain such a call'
        (test_ledger_is_append_only_in_code.py): `canonical()` is documented
        as 'NFKC, then casefold, then strip' — a second canonicaliser
        anywhere else in `mvp/` or `dynamo/` would almost certainly reach for
        `unicodedata.normalize(\"NFKC\", ...)` too, which is the seam (S3)
        P1.2 exists to close before PR 4 can inherit it.
        """
        offenders = []
        for root in (_BACKEND_ROOT / "mvp", _BACKEND_ROOT / "dynamo"):
            for path in root.rglob("*.py"):
                if path == _TASK_TAG_MODULE:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
                if "NFKC" in text:
                    offenders.append(str(path.relative_to(_BACKEND_ROOT)))
        assert not offenders, (
            "a second tag/id canonicaliser appears outside mvp/task_tag.py: "
            + ", ".join(offenders)
        )


# ---------------------------------------------------------------------------
# P1.2 — canonical() is idempotent (the named property test)
# ---------------------------------------------------------------------------

class TestCanonicalIdempotent:
    @DEFAULT_SETTINGS
    @given(raw=st.text(max_size=200))
    def test_canonical_is_idempotent(self, raw):
        once = canonical(raw)
        twice = canonical(once)
        assert once == twice

    def test_canonical_lowercases_and_strips(self):
        assert canonical("  Build-123  ") == "build-123"

    def test_canonical_never_raises(self):
        for bad in ("", "\x00\x01\x02", "a" * 10_000, "　　"):
            canonical(bad)  # must not raise


# ---------------------------------------------------------------------------
# P1.2 / P1.3 — resolve() is total (never raises) over arbitrary input
# ---------------------------------------------------------------------------

class TestResolveIsTotal:
    @DEFAULT_SETTINGS
    @given(raw=st.one_of(st.none(), st.text(max_size=5_000)))
    def test_resolve_never_raises(self, raw):
        resolve(raw)  # must not raise for ANY string or None

    @DEFAULT_SETTINGS
    @given(raw=st.one_of(st.none(), st.text(max_size=5_000)))
    def test_resolve_result_shape_is_always_valid(self, raw):
        """Whatever comes back, it is a (tag, Source) pair where the tag is
        either the sentinel or a GRAMMAR-conforming, <=MAX_LEN string — never
        something the DynamoDB key grammar would reject."""
        tag, source = resolve(raw)
        assert isinstance(tag, str) and tag != ""
        assert isinstance(source, Source)
        assert len(tag) <= MAX_LEN
        assert tag == SENTINEL or GRAMMAR.match(tag)


# ---------------------------------------------------------------------------
# P1.3 — malformed / over-long / control-character / '#' tags never raise,
# and are recorded as dropped_grammar (never ASSERTED, never an exception).
# ---------------------------------------------------------------------------

class TestMalformedTagsDropNotRaise:
    @pytest.mark.parametrize("bad", [
        "has space",
        "tag#name",              # DynamoDB key delimiter
        "a" * 65,                # one over MAX_LEN
        "tag\x00name",           # NUL
        "tag\x1fname",           # unit separator control char
        "tag\rname",             # CR
        "tag\nname",             # LF
        "semi;colon",
        "quote\"x",
        "slash/y",
    ])
    def test_dropped_to_sentinel_with_dropped_grammar_source(self, bad):
        tag, source = resolve(bad)
        assert tag == SENTINEL
        assert source is Source.DROPPED_GRAMMAR

    def test_exactly_max_len_is_accepted_not_dropped(self):
        ok = "a" * MAX_LEN
        tag, source = resolve(ok)
        assert tag == ok
        assert source is Source.ASSERTED

    def test_one_over_max_len_is_dropped(self):
        tag, source = resolve("a" * (MAX_LEN + 1))
        assert tag == SENTINEL
        assert source is Source.DROPPED_GRAMMAR


# ---------------------------------------------------------------------------
# P1.3 — absence (no header) and presence-but-empty both resolve to ABSENT,
# never to DROPPED_GRAMMAR and never to an exception.
# ---------------------------------------------------------------------------

class TestAbsentAndBlank:
    def test_header_absent_is_absent(self):
        """rule #2 negative case: header absent."""
        tag, source = resolve(None)
        assert tag == SENTINEL
        assert source is Source.ABSENT

    def test_header_present_but_empty_is_absent(self):
        """rule #2 negative case: header present but empty."""
        tag, source = resolve("")
        assert tag == SENTINEL
        assert source is Source.ABSENT

    def test_header_present_but_whitespace_only_is_absent(self):
        """Not literally required by rule #2, but the sibling correlation-id
        module (mvp.observability.context._validate) treats a present,
        whitespace-only header as absent ('empty ≡ absent' — a client that
        sends the header with nothing meaningful in it plainly means no
        tag). `resolve`'s docstring says 'None or blank'; this test reads
        'blank' as including whitespace-only, following that sibling
        precedent. Flagged in the handoff report as an interpretation, not a
        certainty, in case the other worker read 'blank' as `== \"\"` only.
        """
        tag, source = resolve("   ")
        assert tag == SENTINEL
        assert source is Source.ABSENT


# ---------------------------------------------------------------------------
# P1.4 — the reserved sentinel, in ANY casing, can never be asserted.
# ---------------------------------------------------------------------------

class TestSentinelIsReserved:
    @pytest.mark.parametrize("bad", [
        "unlabelled",
        "UNLABELLED",
        "Unlabelled",
        "UnLaBeLLeD",
        "  unlabelled  ",
        "  UNLABELLED  ",
    ])
    def test_asserted_sentinel_in_any_casing_drops_not_asserts(self, bad):
        tag, source = resolve(bad)
        assert tag == SENTINEL
        assert source is Source.DROPPED_GRAMMAR, (
            f"resolve({bad!r}) must never report Source.ASSERTED for the "
            "reserved sentinel — an asserted 'Unlabelled' merging with "
            "genuinely untagged spend is exactly what P1.4 exists to block"
        )

    def test_a_real_tag_that_merely_contains_unlabelled_is_still_asserted(self):
        """Non-vacuity: the reservation is on the CANONICAL FORM equalling
        the sentinel exactly, not on the substring appearing anywhere — a
        tag like 'pre-unlabelled-migration' is a legitimate asserted tag."""
        tag, source = resolve("pre-unlabelled-migration")
        assert tag == "pre-unlabelled-migration"
        assert source is Source.ASSERTED
