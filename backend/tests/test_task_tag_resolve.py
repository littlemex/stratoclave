"""Tests for `mvp.task_tag`: the single place a client-supplied task tag is
canonicalised, validated, and resolved to a value safe to persist.

`canonical()` and `resolve()` must be the ONLY place a tag is normalised
anywhere in this codebase, and `GRAMMAR` must be the exact same compiled
pattern the correlation-id module already validates client headers with —
not a recompiled copy of its text — so the two grammars cannot drift apart
from each other the next time either one is edited.

`resolve()` must never raise, for any input: a malformed, over-long,
control-character-bearing, or `#`-bearing tag has to resolve to "no tag was
recorded" rather than end the request, because a tag that can refuse a
request has no safe place to sit relative to a money reservation. The
HTTP-level half of that guarantee — the endpoint itself returns the same
status with or without a tag — is in `test_task_tag_never_refuses.py`.

The reserved sentinel (`"unlabelled"`) can never be the CANONICAL FORM of an
asserted tag, in any casing: if it could, a caller who deliberately typed
"Unlabelled" would be indistinguishable from a caller who sent nothing at
all, which would corrupt the one grouping this tag exists to support.

`mvp.task_tag` does not exist at the base commit, so every test below fails
on `ModuleNotFoundError`.
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
# Sentinel / grammar sanity, and the "declared once" structural checks.
# ---------------------------------------------------------------------------

class TestDeclaredOnce:
    def test_sentinel_is_the_literal_string_unlabelled(self):
        assert SENTINEL == "unlabelled"

    def test_max_len_is_64(self):
        assert MAX_LEN == 64

    def test_header_name_is_x_sc_task_tag(self):
        assert HDR_TASK_TAG == "x-sc-task-tag"

    def test_grammar_is_the_correlation_id_grammar_reused_not_copied(self):
        """GRAMMAR must be the SAME compiled Pattern object the
        correlation-id module already validates client headers with, not a
        second `re.compile` of an identical-looking pattern string — a copy
        can silently drift from the original the next time either one is
        edited, while sharing the one object cannot.
        """
        assert GRAMMAR is _ID_GRAMMAR

    def test_no_other_module_defines_its_own_nfkc_normaliser(self):
        """`canonical()` is documented as 'NFKC, then casefold, then strip'
        and is the only place a tag is ever normalised. A second
        canonicaliser anywhere else in `mvp/` or `dynamo/` would almost
        certainly reach for `unicodedata.normalize(\"NFKC\", ...)` too, and
        two independently maintained normalisers are a defect even while
        they happen to agree, because nothing keeps them agreeing tomorrow.
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
# canonical() is idempotent.
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
# resolve() is total (never raises) over arbitrary input.
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
# Malformed / over-long / control-character / '#' tags never raise, and are
# recorded as dropped_grammar (never ASSERTED, never an exception).
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
# Absence (no header) and presence-but-empty-or-blank both resolve to
# ABSENT, never to DROPPED_GRAMMAR and never to an exception.
# ---------------------------------------------------------------------------

class TestAbsentAndBlank:
    def test_header_absent_is_absent(self):
        """No header at all resolves to the sentinel with source ABSENT."""
        tag, source = resolve(None)
        assert tag == SENTINEL
        assert source is Source.ABSENT

    def test_header_present_but_empty_is_absent(self):
        """A header sent with an empty value means the same thing as no
        header at all, not a malformed value."""
        tag, source = resolve("")
        assert tag == SENTINEL
        assert source is Source.ABSENT

    @pytest.mark.parametrize("blank", ["   ", "\t", "\t\r\n", " 　"])
    def test_header_present_but_whitespace_only_is_absent(self, blank):
        """A header that is present but carries only whitespace means the
        same thing as no header at all, matching the sibling correlation-id
        module's treatment of client headers: a client sending nothing
        meaningful plainly means no tag, not a malformed one."""
        tag, source = resolve(blank)
        assert tag == SENTINEL
        assert source is Source.ABSENT


# ---------------------------------------------------------------------------
# The reserved sentinel, in ANY casing, can never be asserted -- and WHY it
# was dropped is distinguishable from a genuinely malformed tag.
# ---------------------------------------------------------------------------

class TestSentinelIsReserved:
    """Typing the reserved word on purpose and sending a malformed header
    are two different mistakes, and conflating them was silent: a team
    whose project is actually called "Unlabelled" had every call recorded,
    never refused, and the tag vanished with no error and no row under that
    name -- indistinguishable from a header that simply failed the grammar.

    `resolve()` checks the canonical form against the sentinel BEFORE the
    length and grammar checks, so the reserved word reports
    `Source.DROPPED_RESERVED` while a genuinely malformed value still
    reports `Source.DROPPED_GRAMMAR` — two different reasons behind the
    same recorded value. The response carries `x-sc-task-tag-dropped` with
    `reserved` or `grammar` so the caller learns which happened at the
    moment it happens; those two words are exactly what these two source
    values are for.
    """

    @pytest.mark.parametrize("bad", [
        "unlabelled",
        "UNLABELLED",
        "Unlabelled",
        "UnLaBeLLeD",
        "  unlabelled  ",
        "  UNLABELLED  ",
    ])
    def test_asserted_sentinel_in_any_casing_drops_as_reserved(self, bad):
        tag, source = resolve(bad)
        assert tag == SENTINEL
        assert source is Source.DROPPED_RESERVED, (
            f"resolve({bad!r}) must never report Source.ASSERTED for the "
            "reserved sentinel, and must report DROPPED_RESERVED "
            "specifically, not DROPPED_GRAMMAR — an asserted 'Unlabelled' "
            "merging with genuinely untagged spend is exactly what this "
            "reservation exists to block, and the caller can only learn "
            "which mistake happened if the two reasons stay distinct"
        )

    def test_a_genuinely_malformed_tag_still_drops_as_grammar_not_reserved(self):
        """Guards against the two reasons collapsing back into one: a value
        that is malformed but is NOT the reserved word must still report
        DROPPED_GRAMMAR. A future change that makes the reserved-word check
        too broad, or that checks it instead of (rather than before) the
        grammar check, could otherwise report every dropped tag as
        "reserved" and lose this distinction entirely."""
        tag, source = resolve("has space")
        assert tag == SENTINEL
        assert source is Source.DROPPED_GRAMMAR

    @pytest.mark.parametrize("bad,expected_source", [
        ("unlabelled", Source.DROPPED_RESERVED),
        ("has space", Source.DROPPED_GRAMMAR),
    ])
    def test_both_dropped_sources_still_record_the_same_sentinel_tag(
        self, bad, expected_source,
    ):
        """The recorded VALUE is unchanged either way — only the REASON is
        newly distinguishable. A reader who looks only at `task_tag` must
        see the identical sentinel for a reserved word and for a malformed
        value; the whole distinction lives in `task_tag_source`, never in
        `task_tag` itself."""
        tag, source = resolve(bad)
        assert tag == SENTINEL
        assert source is expected_source

    def test_a_real_tag_that_merely_contains_unlabelled_is_still_asserted(self):
        """Non-vacuity: the reservation is on the CANONICAL FORM equalling
        the sentinel exactly, not on the substring appearing anywhere — a
        tag like 'pre-unlabelled-migration' is a legitimate asserted tag."""
        tag, source = resolve("pre-unlabelled-migration")
        assert tag == "pre-unlabelled-migration"
        assert source is Source.ASSERTED
