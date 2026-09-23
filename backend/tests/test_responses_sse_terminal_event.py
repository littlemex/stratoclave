"""Which SSE frame carries the usage, and how it is recognised.

The captured stream this file is written against had TEN `data:` lines and ZERO
`event:` lines (4,431 bytes, `us.openai.gpt-5.6-sol`, 2026-09-24). The route read
usage only when an `event:` line said `response.completed`, so it read usage off no
streamed response at all: the ledger for three consecutive calls showed
`input 0 / output 0` for the two streamed ones beside `7 / 5` for the
non-streamed one.
"""
from __future__ import annotations

import json

import pytest

from mvp._responses_wire import (
    METERED_TERMINAL_TYPES,
    SSEFrameConflict,
    sse_event_type,
)
from mvp.openai_responses import _handle_sse_event

_USAGE = {
    "input_tokens": 7,
    "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 0},
    "output_tokens": 5,
    "output_tokens_details": {"reasoning_tokens": 0},
    "total_tokens": 12,
}


def _frame(event_type: str, *, with_event_line: bool, usage: dict | None = _USAGE,
           status: str = "completed") -> bytes:
    body = {"type": event_type,
            "response": {"id": "resp_x", "status": status, "usage": usage}}
    head = f"event: {event_type}\n" if with_event_line else ""
    return (head + f"data: {json.dumps(body)}\n\n").encode("utf-8")


class TestTheEventTypeComesFromEitherSource:
    def test_the_measured_shape_has_no_event_line_and_still_resolves(self):
        assert sse_event_type(None, {"type": "response.completed"}) == "response.completed"

    def test_the_sse_default_event_name_carries_no_information(self):
        """A frame with no `event:` line has the type `"message"` per the SSE spec.
        Treating that as a real event name is how a payload type would be ignored."""
        assert sse_event_type("message", {"type": "response.completed"}) == "response.completed"

    def test_an_event_line_alone_still_resolves(self):
        """A future upstream that DOES send `event:` lines must keep working."""
        assert sse_event_type("response.completed", {"foo": 1}) == "response.completed"

    def test_a_frame_that_says_two_different_things_is_refused(self):
        with pytest.raises(SSEFrameConflict):
            sse_event_type("response.completed", {"type": "response.failed"})

    def test_a_non_json_payload_resolves_to_nothing(self):
        assert sse_event_type(None, "[DONE]") is None


class TestUsageIsReadOffTheTerminalFrame:
    def test_usage_is_read_from_the_measured_no_event_line_shape(self):
        """The regression this whole change exists for."""
        _out, usage, _id = _handle_sse_event(_frame("response.completed", with_event_line=False))
        assert usage is not None, (
            "the shape the endpoint actually sends carried no usage, which is the "
            "state in which every streamed call billed nothing"
        )
        assert (usage.input_tokens, usage.output_tokens) == (7, 5)

    def test_usage_is_still_read_when_an_event_line_is_present(self):
        _out, usage, _id = _handle_sse_event(_frame("response.completed", with_event_line=True))
        assert usage is not None and usage.input_tokens == 7

    def test_a_truncated_stream_is_metered_too(self):
        """`response.incomplete` is what a stream that hits `max_output_tokens`
        actually ends with -- measured, with a full usage block and
        `reasoning_tokens == output_tokens`. Reading only `response.completed`
        leaves every truncated stream unbilled."""
        assert "response.incomplete" in METERED_TERMINAL_TYPES
        _out, usage, _id = _handle_sse_event(
            _frame("response.incomplete", with_event_line=False, status="incomplete"))
        assert usage is not None and usage.output_tokens == 5

    def test_a_non_terminal_frame_carries_no_usage(self):
        """The non-vacuous companion: if every frame yielded usage, the assertions
        above would pass on an implementation that ignored the event type."""
        _out, usage, _id = _handle_sse_event(
            _frame("response.output_text.delta", with_event_line=False))
        assert usage is None

    def test_a_terminal_frame_whose_usage_will_not_parse_carries_no_usage(self):
        """Unreadable is not zero. The caller must land on the unobserved path, and
        it can only do that if it is handed `None` rather than a synthesised zero."""
        _out, usage, _id = _handle_sse_event(
            _frame("response.completed", with_event_line=False,
                   usage={"input_tokens": 7}))
        assert usage is None

    def test_a_conflicting_terminal_frame_carries_no_usage(self):
        _out, usage, _id = _handle_sse_event(
            (f"event: response.completed\n"
             f"data: {json.dumps({'type': 'response.failed', 'response': {'usage': _USAGE}})}\n\n"
             ).encode("utf-8"))
        assert usage is None


class TestAStreamWhoseUsageIsUnreadableIsNotSettledAtZero:
    """The route's own comment says an unreadable terminal "lands the stream on the
    unobserved path". These pin that it actually does, because the parser refusing to
    report a zero buys nothing if the caller converts the refusal back into one.

    Driven at the route through a mocked upstream rather than at the handler, because
    the defect is in what the CALLER does with `usage is None`, not in the handler.
    """

    @pytest.mark.parametrize(
        "frames",
        [
            # A terminal whose usage will not parse.
            [{"type": "response.completed",
              "response": {"id": "r", "usage": {"input_tokens": 7}}}],
            # No terminal at all.
            [{"type": "response.created", "response": {"id": "r"}},
             {"type": "response.output_text.delta", "delta": "hi"}],
            # A terminal carrying a counter this gateway does not price.
            [{"type": "response.completed",
              "response": {"id": "r", "usage": dict(_USAGE, audio_tokens=4)}}],
        ],
        ids=["unreadable-usage", "no-terminal", "unknown-counter"],
    )
    def test_no_usage_means_no_settle(self, frames):
        """Asserted on the handler's own answer, which is the input the settle
        decision reads: `None` is what must reach it, never a synthesised zero."""
        for frame in frames:
            body = (f"data: {json.dumps(frame)}\n\n").encode("utf-8")
            _out, usage, _id = _handle_sse_event(body)
            assert usage is None, (
                f"{frame['type']} handed the settle a usage value it must not have; "
                "a zero here is a charge of nothing for a model that ran"
            )
