"""Unit tests for the Converse invoker's event normalization (_converse_core).

These pin, in isolation (no DynamoDB, no budget), that:
  - `_aiter_blocking_stream` still yields in order, terminates cleanly on
    StopIteration, and offloads each `next()` to a worker thread (moved VERBATIM
    from mvp.anthropic — same guarantees the pre-move regression tests asserted);
  - `normalized_events` maps a Bedrock converse_stream event sequence to the
    wire-agnostic StreamEvent sequence with the SAME observable behaviour as
    today's `_stream_messages` loop: non-empty text deltas only, raw stop reason,
    usage totals with cache tokens.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from mvp import _converse_core as core
from mvp import _converse_types as t


def _run(agen_factory):
    async def collect():
        out = []
        async for ev in agen_factory():
            out.append(ev)
        return out

    return asyncio.run(collect())


# --- _aiter_blocking_stream: order / termination / thread-offload -----------
def test_aiter_yields_in_order():
    events = [
        {"contentBlockDelta": {"delta": {"text": "a"}}},
        {"messageStop": {"stopReason": "end_turn"}},
    ]
    got = _run(lambda: core._aiter_blocking_stream(iter(events)))
    assert got == events


def test_aiter_terminates_on_stopiteration_without_runtimeerror():
    assert _run(lambda: core._aiter_blocking_stream(iter([]))) == []


def test_aiter_offloads_next_to_worker_thread():
    main_tid = threading.get_ident()
    seen: list[int] = []

    class TracingIter:
        def __init__(self):
            self._n = 0

        def __iter__(self):
            return self

        def __next__(self):
            seen.append(threading.get_ident())
            self._n += 1
            if self._n > 2:
                raise StopIteration
            return {"contentBlockDelta": {"delta": {"text": "x"}}}

    _run(lambda: core._aiter_blocking_stream(TracingIter()))
    assert seen, "iterator must have been advanced"
    assert all(tid != main_tid for tid in seen), "every next() must run off the loop"


# --- normalized_events: faithful mapping of a Bedrock stream ----------------
def test_normalized_events_maps_text_stop_and_usage():
    source = iter(
        [
            {"contentBlockDelta": {"delta": {"text": "he"}}},
            {"contentBlockDelta": {"delta": {"text": ""}}},  # empty -> dropped
            {"contentBlockDelta": {"delta": {"text": "llo"}}},
            {"messageStop": {"stopReason": "tool_use"}},
            {
                "metadata": {
                    "usage": {
                        "inputTokens": 12,
                        "outputTokens": 3,
                        "cacheReadInputTokens": 4,
                        "cacheWriteInputTokens": 0,
                    }
                }
            },
        ]
    )
    got = _run(lambda: core.normalized_events(source))

    # Two text deltas (the empty one is dropped, as today's `if text:` guard).
    text_deltas = [e for e in got if isinstance(e, t.ContentTextDelta)]
    assert [d.text for d in text_deltas] == ["he", "llo"]
    assert all(d.index == 0 for d in text_deltas)

    stops = [e for e in got if isinstance(e, t.MessageStop)]
    assert len(stops) == 1
    # RAW Bedrock reason preserved; adapter maps it (as _map_stop_reason did).
    assert stops[0].stop_reason == "tool_use"

    usages = [e for e in got if isinstance(e, t.Usage)]
    assert len(usages) == 1
    assert (usages[0].input, usages[0].output) == (12, 3)
    assert (usages[0].cache_read, usages[0].cache_write) == (4, 0)


def test_normalized_events_accumulator_reproduces_todays_usage():
    """Feeding the normalized events through UsageAccumulator must land the same
    totals today's inline loop accumulated (single metadata event, index 0).
    """
    source = iter(
        [
            {"contentBlockDelta": {"delta": {"text": "hi"}}},
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": {"usage": {"inputTokens": 7, "outputTokens": 2}}},
        ]
    )
    acc = t.UsageAccumulator()
    for ev in _run(lambda: core.normalized_events(source)):
        acc.absorb(ev)
    assert (acc.input_tokens, acc.output_tokens) == (7, 2)
    assert acc.stop_reason == "end_turn"


# ---------------------------------------------------------------------------
# C8.1: absent/unreadable usage must yield NO Usage event, never a zero one.
#
# `usage_from_bedrock` is the single function both transports must call; its
# absence at bb0fb2c means every test below fails at collection/call time
# (AttributeError), which is the correct failure for a contract not yet
# implemented. Imports are function-local so a missing symbol fails only the
# test that needs it, not the whole module (the pre-existing tests above stay
# green while these are red).
# ---------------------------------------------------------------------------


class TestUsageFromBedrockAbsenceIsNotZero:
    """`usage_from_bedrock` must return None — not a Usage(0, 0, ...) — for
    every shape where the provider's usage cannot be read. A test asserting
    `usage.output == 0` would pass against the defect (int(usage.get(..., 0))
    also produces 0); asserting `is None` is what actually pins the fix.
    """

    def test_none_usage_returns_none(self):
        from mvp._converse_core import usage_from_bedrock

        assert usage_from_bedrock(None) is None

    def test_empty_dict_usage_returns_none(self):
        from mvp._converse_core import usage_from_bedrock

        assert usage_from_bedrock({}) is None

    def test_missing_output_tokens_returns_none(self):
        from mvp._converse_core import usage_from_bedrock

        assert usage_from_bedrock({"inputTokens": 12}) is None

    def test_missing_input_tokens_returns_none(self):
        from mvp._converse_core import usage_from_bedrock

        assert usage_from_bedrock({"outputTokens": 3}) is None

    def test_non_integer_output_tokens_returns_none(self):
        """A malformed field must not be coerced through int(); a coercion that
        raised inside the old `int(usage.get(...))` call is not this defect's
        shape, but silently accepting a non-int (e.g. a stringified float) as
        a token count would be."""
        from mvp._converse_core import usage_from_bedrock

        assert usage_from_bedrock({"inputTokens": 10, "outputTokens": "four"}) is None

    def test_negative_output_tokens_returns_none(self):
        from mvp._converse_core import usage_from_bedrock

        assert usage_from_bedrock({"inputTokens": 10, "outputTokens": -1}) is None

    def test_well_formed_usage_returns_a_usage(self):
        """Positive control: the function is not vacuously returning None for
        everything."""
        from mvp._converse_core import usage_from_bedrock

        got = usage_from_bedrock({"inputTokens": 10, "outputTokens": 4})
        assert got is not None
        assert (got.input, got.output) == (10, 4)

    def test_cache_legs_stay_optional_int_on_a_well_formed_usage(self):
        """Real measured shape: two calls over one 3,524-token cached prefix.
        Call 1 writes the cache, call 2 reads it; `inputTokens`/`outputTokens`
        are the SAME small numbers on both calls — the cache legs are
        disjoint from `inputTokens`, not folded into it."""
        from mvp._converse_core import usage_from_bedrock

        call1 = usage_from_bedrock({
            "inputTokens": 10, "outputTokens": 4,
            "cacheWriteInputTokens": 3524, "cacheReadInputTokens": 0,
        })
        assert (call1.input, call1.output) == (10, 4)
        assert (call1.cache_write, call1.cache_read) == (3524, 0)

        call2 = usage_from_bedrock({
            "inputTokens": 10, "outputTokens": 4,
            "cacheWriteInputTokens": 0, "cacheReadInputTokens": 3524,
        })
        assert (call2.input, call2.output) == (10, 4)
        assert (call2.cache_write, call2.cache_read) == (0, 3524)


@pytest.mark.parametrize(
    "event",
    [
        pytest.param({"metadata": {}}, id="metadata_no_usage_key"),
        pytest.param({"metadata": {"usage": {}}}, id="metadata_empty_usage"),
        pytest.param(
            {"metadata": {"cacheDetails": {"some": "thing"}}},
            id="metadata_cacheDetails_only__the_key_that_appeared_and_vanished",
        ),
        pytest.param(
            {"metadata": {"usage": {"inputTokens": 10}}},
            id="usage_missing_outputTokens",
        ),
        pytest.param(
            {"metadata": {"usage": {"outputTokens": 4}}},
            id="usage_missing_inputTokens",
        ),
    ],
)
def test_normalized_events_yields_no_usage_event_for_unreadable_usage(event):
    """The defect this pins: today's code does `int(usage.get("inputTokens", 0))`
    unconditionally, so every one of these shapes currently yields
    Usage(input=0, output=0, ...) — a plausible-looking zero settle for a call
    whose usage the gateway could not read. Post-fix, `normalized_events` must
    yield NOTHING for the metadata branch in every one of these cases, so the
    stream ends unobserved and `STRATOCLAVE_UNOBSERVED_HOLDS` policy applies
    instead of a phantom zero settle.
    """
    got = _run(lambda: core.normalized_events(iter([event])))
    usages = [e for e in got if isinstance(e, t.Usage)]
    assert usages == [], (
        f"expected no Usage event for {event!r}, got {usages!r} — "
        "a zero-valued Usage is exactly the corruption this pins"
    )


def test_normalized_events_still_yields_usage_for_a_well_formed_metadata_event():
    """Positive control for the parametrized test above: a well-formed usage
    block must still produce exactly one Usage event. Without this, a fix that
    made normalized_events drop the metadata branch UNCONDITIONALLY would pass
    every negative case above for the wrong reason."""
    got = _run(lambda: core.normalized_events(
        iter([{"metadata": {"usage": {"inputTokens": 5, "outputTokens": 2}}}])
    ))
    usages = [e for e in got if isinstance(e, t.Usage)]
    assert len(usages) == 1
    assert (usages[0].input, usages[0].output) == (5, 2)


# ---------------------------------------------------------------------------
# C13.4: reasoning legs — REASONING_LEGS declared once, each leg an
# independent `if` (never an `elif` chain), unknown legs warn once per
# stream via a set difference against the one declaration.
# ---------------------------------------------------------------------------


def test_reasoning_legs_is_the_declared_frozenset():
    from mvp._converse_core import REASONING_LEGS

    assert REASONING_LEGS == frozenset({"text", "signature", "redactedContent"})
    assert isinstance(REASONING_LEGS, frozenset)


def test_normalized_events_reasoning_text_delta_is_normalized():
    source = iter([
        {"contentBlockDelta": {"delta": {"reasoningContent": {"text": "thinking..."}}}},
    ])
    got = _run(lambda: core.normalized_events(source))
    reasoning = [e for e in got if isinstance(e, t.ContentReasoningDelta)]
    assert len(reasoning) == 1
    assert (reasoning[0].kind, reasoning[0].value) == ("text", "thinking...")


def test_normalized_events_reasoning_signature_delta_is_normalized():
    source = iter([
        {"contentBlockDelta": {"delta": {"reasoningContent": {"signature": "sig-xyz"}}}},
    ])
    got = _run(lambda: core.normalized_events(source))
    reasoning = [e for e in got if isinstance(e, t.ContentReasoningDelta)]
    assert len(reasoning) == 1
    assert (reasoning[0].kind, reasoning[0].value) == ("signature", "sig-xyz")


def test_normalized_events_reasoning_redacted_content_is_normalized():
    source = iter([
        {"contentBlockDelta": {"delta": {"reasoningContent": {"redactedContent": "already-b64"}}}},
    ])
    got = _run(lambda: core.normalized_events(source))
    reasoning = [e for e in got if isinstance(e, t.ContentReasoningDelta)]
    assert len(reasoning) == 1
    assert reasoning[0].kind == "redacted"


def test_normalized_events_redacted_content_bytes_are_base64_encoded():
    """Interface: "redactedContent is base64 when it arrives as bytes." Bedrock's
    SDK can hand back raw bytes for this field; the normalized value must be a
    base64 STRING, not the raw bytes object, so every downstream consumer of
    StreamEvent (which is plain data, JSON-rendered by the adapters) can carry
    it without a type-specific branch of its own."""
    import base64

    raw = b"\x00\x01\xfe\xff redacted payload"
    source = iter([
        {"contentBlockDelta": {"delta": {"reasoningContent": {"redactedContent": raw}}}},
    ])
    got = _run(lambda: core.normalized_events(source))
    reasoning = [e for e in got if isinstance(e, t.ContentReasoningDelta)]
    assert len(reasoning) == 1
    assert reasoning[0].kind == "redacted"
    assert isinstance(reasoning[0].value, str)
    assert base64.b64decode(reasoning[0].value) == raw


def test_normalized_events_reasoning_deltas_many_text_one_signature():
    """The measured shape: 90 text deltas, then exactly one signature delta.
    All 91 must survive as independent ContentReasoningDelta events."""
    source = iter(
        [{"contentBlockDelta": {"delta": {"reasoningContent": {"text": f"t{i}"}}}} for i in range(90)]
        + [{"contentBlockDelta": {"delta": {"reasoningContent": {"signature": "the-signature"}}}}]
    )
    got = _run(lambda: core.normalized_events(source))
    reasoning = [e for e in got if isinstance(e, t.ContentReasoningDelta)]
    assert len(reasoning) == 91
    assert [r.kind for r in reasoning].count("text") == 90
    sig = [r for r in reasoning if r.kind == "signature"]
    assert len(sig) == 1 and sig[0].value == "the-signature"


def test_normalized_events_one_delta_with_empty_text_and_a_signature_yields_the_signature():
    """The harder shape the contract calls out: ONE delta carrying an empty
    `text` and a non-empty `signature` together. An `elif` chain keyed on key
    PRESENCE (`if "text" in reasoning: ... elif "signature" in reasoning: ...`)
    takes the text branch first (the key is present, even though its value is
    empty) and never reaches the signature — dropping the leg that carries the
    thinking block's cryptographic continuity. Independent `if`s, each guarded
    on the VALUE being truthy (matching the existing `if text:` convention for
    plain text deltas), must not have this failure mode: the empty text leg
    yields nothing, and the signature leg still yields.
    """
    source = iter([
        {"contentBlockDelta": {"delta": {"reasoningContent": {"text": "", "signature": "sig-abc"}}}},
    ])
    got = _run(lambda: core.normalized_events(source))
    reasoning = [e for e in got if isinstance(e, t.ContentReasoningDelta)]
    kinds = {r.kind: r.value for r in reasoning}
    assert kinds.get("signature") == "sig-abc", (
        f"the signature leg must survive an empty co-occurring text leg, got {reasoning!r}"
    )
    assert "text" not in kinds, (
        "an empty text leg must not itself yield a (misleading, empty) text delta — "
        "same 'if text:' convention as the plain-text branch above"
    )


def test_normalized_events_block_start_auto_emits_for_a_reasoning_first_leg():
    """"the block-start auto-emit covers a delta whose first leg is not text."
    Today's code only auto-emits ContentBlockStart inside the `if text:`
    branch, so a stream whose very first delta at an unseen index is a
    reasoning leg (no prior contentBlockStart, as Bedrock sends for text)
    would otherwise never get a ContentBlockStart at all."""
    source = iter([
        {"contentBlockDelta": {"delta": {"reasoningContent": {"text": "hmm"}}}},
    ])
    got = _run(lambda: core.normalized_events(source))
    starts = [e for e in got if isinstance(e, t.ContentBlockStart)]
    assert len(starts) == 1, f"expected exactly one auto-emitted block start, got {got!r}"
    assert starts[0].index == 0
    assert starts[0].block_type != "text", (
        "a reasoning-first block should not be mis-typed as text — the whole "
        "point of carrying block_type is so an adapter never has to infer it"
    )


class TestUnknownReasoningLegWarnsOnceViaSetDifference:
    """Unknown legs warn once per stream, naming the sorted set difference.

    The trap: an implementation that infers "warn when we recognized nothing"
    would stay silent here, because a KNOWN leg (`text`) is present in the SAME
    delta as the unknown one and something IS emitted. The set-difference
    check must fire regardless of whether anything known co-occurred.
    """

    def test_unknown_leg_alongside_a_known_leg_still_warns(self):
        from structlog.testing import capture_logs

        source = iter([
            {"contentBlockDelta": {"delta": {"reasoningContent": {
                "text": "hi", "cargoLeg": "unexpected-value",
            }}}},
        ])
        with capture_logs() as logs:
            _run(lambda: core.normalized_events(source))
        warnings = [e for e in logs if e.get("log_level") == "warning"]
        assert len(warnings) == 1, (
            f"expected exactly one warning for one unknown leg, got {warnings!r}"
        )
        assert any("cargoLeg" in str(v) for v in warnings[0].values()), (
            f"the warning must name the unknown leg, got {warnings[0]!r}"
        )

    def test_unknown_leg_warns_exactly_once_per_stream_even_if_repeated(self):
        from structlog.testing import capture_logs

        source = iter([
            {"contentBlockDelta": {"delta": {"reasoningContent": {"cargoLeg": "a"}}}},
            {"contentBlockDelta": {"delta": {"reasoningContent": {"cargoLeg": "b"}}}},
            {"contentBlockDelta": {"delta": {"reasoningContent": {"text": "hi", "cargoLeg": "c"}}}},
        ])
        with capture_logs() as logs:
            _run(lambda: core.normalized_events(source))
        warnings = [e for e in logs if e.get("log_level") == "warning"]
        assert len(warnings) == 1, (
            f"the SAME unknown leg recurring in a stream must warn ONCE, not once "
            f"per occurrence — got {len(warnings)}: {warnings!r}"
        )

    def test_only_known_legs_never_warns(self):
        from structlog.testing import capture_logs

        source = iter([
            {"contentBlockDelta": {"delta": {"reasoningContent": {"text": "hi"}}}},
            {"contentBlockDelta": {"delta": {"reasoningContent": {"signature": "sig"}}}},
            {"contentBlockDelta": {"delta": {"reasoningContent": {"redactedContent": "r"}}}},
        ])
        with capture_logs() as logs:
            _run(lambda: core.normalized_events(source))
        warnings = [e for e in logs if e.get("log_level") == "warning"]
        assert warnings == [], f"no unknown leg was present; got warnings {warnings!r}"


# ---------------------------------------------------------------------------
# `answerless_billed` — pure predicate, item 5 of the P1 handoff. Filed under
# the answer-less-billed warning in router-requests/03-impl/HANDOFF.md's table (not C8.1..4), but declared in the SAME
# `_converse_core.py` interface this file already tests and explicitly
# in-scope per the P1 task brief; see the report for the discrepancy note.
# ---------------------------------------------------------------------------


class TestAnswerlessBilled:
    def test_positive_output_no_text_no_tool_use_is_answerless_billed(self):
        from mvp._converse_core import answerless_billed

        assert answerless_billed(
            output_tokens=4097, saw_nonempty_text=False, saw_tool_use=False,
            block_types=frozenset({"reasoning"}),
        ) is True

    def test_the_measured_4097_token_no_answer_fixture_is_answerless_billed(self):
        """Streaming, budget_tokens=4096, maxTokens=4097: stopReason=max_tokens,
        outputTokens=4097, 7,159 characters of reasoning text, 0 characters of
        answer text. Real measured shape."""
        from mvp._converse_core import answerless_billed

        assert answerless_billed(
            output_tokens=4097, saw_nonempty_text=False, saw_tool_use=False,
            block_types=frozenset({"reasoning"}),
        ) is True

    def test_tool_use_only_reply_is_not_answerless_billed(self):
        """The false positive that would make the whole feature useless: a
        tool-use-only reply has no answer TEXT and positive billed output, so a
        predicate that only looked at text would warn on every agentic turn."""
        from mvp._converse_core import answerless_billed

        assert answerless_billed(
            output_tokens=120, saw_nonempty_text=False, saw_tool_use=True,
            block_types=frozenset({"tool_use"}),
        ) is False

    def test_zero_output_tokens_is_not_answerless_billed(self):
        """Nothing was billed, so there is nothing to warn about — the
        predicate must be gated on output_tokens > 0, not fire on an empty or
        cancelled call."""
        from mvp._converse_core import answerless_billed

        assert answerless_billed(
            output_tokens=0, saw_nonempty_text=False, saw_tool_use=False,
            block_types=frozenset(),
        ) is False

    def test_none_output_tokens_is_not_answerless_billed(self):
        """`output_tokens` is `Optional[int]` per the signature; unobserved
        output must not be treated as a positive billed amount."""
        from mvp._converse_core import answerless_billed

        assert answerless_billed(
            output_tokens=None, saw_nonempty_text=False, saw_tool_use=False,
            block_types=frozenset(),
        ) is False

    def test_normal_text_reply_is_not_answerless_billed(self):
        from mvp._converse_core import answerless_billed

        assert answerless_billed(
            output_tokens=42, saw_nonempty_text=True, saw_tool_use=False,
            block_types=frozenset({"text"}),
        ) is False

    def test_text_and_tool_use_together_is_not_answerless_billed(self):
        from mvp._converse_core import answerless_billed

        assert answerless_billed(
            output_tokens=200, saw_nonempty_text=True, saw_tool_use=True,
            block_types=frozenset({"text", "tool_use"}),
        ) is False
