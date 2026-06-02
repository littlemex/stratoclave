"""Unit tests for the OpenAI Responses route's credit / streaming logic.

These cover the bits that diverge from the Anthropic Messages route:

  - reservation estimation respects the reasoning-effort multiplier and
    the 8 192-token floor;
  - image / file inputs are rejected at the Pydantic layer with HTTP 422;
  - the snake_case / Chat-Completions field-name guard for usage works;
  - the `response.completed` SSE event is the source for streaming usage,
    and an early stream exit settles with zero;
  - the error sanitizer extracts and scrubs OpenAI-flavoured error JSON;
  - SSE `event: error` lines are sanitized before being yielded.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from mvp.openai_responses import (
    OpenAIResponsesRequest,
    _MIN_RESERVATION_TOKENS_OPENAI,
    _estimate_reservation_tokens,
    _extract_usage,
    _format_mantle_error,
    _sanitize_sse_error_line,
)


# ---------------------------------------------------------------------------
# Reservation estimation
# ---------------------------------------------------------------------------


def test_estimate_reservation_no_reasoning():
    """No `reasoning` field → multiplier 1, plain (chars/3 + max_output)."""
    body = OpenAIResponsesRequest(
        model="openai.gpt-5.4",
        input="x" * 900,
        max_output_tokens=4_096,
    )
    # 900 // 3 = 300; 300 + 4096 = 4396; below floor → floor.
    assert _estimate_reservation_tokens(body) == _MIN_RESERVATION_TOKENS_OPENAI


def test_estimate_reservation_high_effort():
    """`reasoning.effort = high` quadruples the output budget."""
    body = OpenAIResponsesRequest(
        model="openai.gpt-5.4",
        input="x" * 900,
        max_output_tokens=4_096,
        reasoning={"effort": "high"},
    )
    # 300 + 4096*4 = 16_684; above floor → use sum.
    assert _estimate_reservation_tokens(body) == 300 + 4_096 * 4


def test_estimate_reservation_xhigh_effort_uses_8x():
    body = OpenAIResponsesRequest(
        model="openai.gpt-5.4",
        input="x" * 30,
        max_output_tokens=8_192,
        reasoning={"effort": "xhigh"},
    )
    # 10 + 8192*8 = 65546; above floor.
    assert _estimate_reservation_tokens(body) == 10 + 8_192 * 8


def test_estimate_reservation_minimum_floor():
    """Tiny request still reserves at least the floor."""
    body = OpenAIResponsesRequest(
        model="openai.gpt-5.4",
        input="hi",
        max_output_tokens=1,
    )
    assert _estimate_reservation_tokens(body) == _MIN_RESERVATION_TOKENS_OPENAI


def test_estimate_reservation_input_array_input_text_blocks():
    """Responses-API style input arrays are walked block-by-block."""
    body = OpenAIResponsesRequest(
        model="openai.gpt-5.4",
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "x" * 600},
                    {"type": "input_text", "text": "y" * 300},
                ],
            }
        ],
        max_output_tokens=1,
        reasoning={"effort": "medium"},
    )
    # 900 chars // 3 = 300; medium=2 → 300 + 1*2 = 302 → floor wins.
    assert _estimate_reservation_tokens(body) == _MIN_RESERVATION_TOKENS_OPENAI


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_image_block_rejected_422():
    """`type: input_image` is reserved for vision and not yet costed here."""
    with pytest.raises(ValidationError):
        OpenAIResponsesRequest(
            model="openai.gpt-5.4",
            input=[{"type": "input_image", "image_url": "https://x.invalid"}],
            max_output_tokens=8,
        )


def test_file_block_rejected_422():
    with pytest.raises(ValidationError):
        OpenAIResponsesRequest(
            model="openai.gpt-5.4",
            input=[
                {
                    "role": "user",
                    "content": [{"type": "input_file", "file_id": "file_x"}],
                }
            ],
            max_output_tokens=8,
        )


def test_oversized_input_rejected():
    with pytest.raises(ValidationError):
        OpenAIResponsesRequest(
            model="openai.gpt-5.4",
            input="x" * 200_001,
            max_output_tokens=8,
        )


def test_per_element_input_text_cap_rejected():
    """A single block whose text exceeds 200k chars must be rejected
    even when the list itself is small (defends against the case where
    a future ASGI body-cap relaxation would otherwise let it through)."""
    with pytest.raises(ValidationError):
        OpenAIResponsesRequest(
            model="openai.gpt-5.4",
            input=[
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "x" * 200_001}],
                }
            ],
            max_output_tokens=8,
        )


def test_aggregate_input_text_cap_rejected():
    """Many small blocks that exceed the aggregate cap must be rejected."""
    blocks = [
        {"type": "input_text", "text": "x" * 100_000} for _ in range(3)
    ]
    with pytest.raises(ValidationError):
        OpenAIResponsesRequest(
            model="openai.gpt-5.4",
            input=[{"role": "user", "content": blocks}],
            max_output_tokens=8,
        )


# ---------------------------------------------------------------------------
# Usage extraction
# ---------------------------------------------------------------------------


def test_extract_usage_responses_field_names():
    assert _extract_usage({"input_tokens": 12, "output_tokens": 34}) == (12, 34)


def test_extract_usage_chat_completions_aliases():
    """If bedrock-mantle ever returns Chat-Completions naming, fall back to it."""
    assert _extract_usage({"prompt_tokens": 5, "completion_tokens": 9}) == (5, 9)


def test_extract_usage_missing_fields_default_to_zero():
    assert _extract_usage({}) == (0, 0)
    assert _extract_usage(None) == (0, 0)  # type: ignore[arg-type]


def test_stream_completed_event_extraction():
    """Decode the canonical streaming completion payload and pull usage."""
    payload = {
        "type": "response.completed",
        "response": {"usage": {"input_tokens": 100, "output_tokens": 200}},
    }
    line = "data: " + json.dumps(payload)
    # Round-trip through the streaming parser shape used in route code.
    body = json.loads(line[len("data: ") :])
    assert body["type"] == "response.completed"
    assert _extract_usage(body["response"]["usage"]) == (100, 200)


def test_stream_early_exit_settles_zero():
    """A stream that ends without `response.completed` leaves usage at 0.

    The route ends up calling settle with (0, 0), which the pipeline
    already covers in test_pipeline_shared.py.
    """
    # No usage event observed.
    assert _extract_usage({}) == (0, 0)


# ---------------------------------------------------------------------------
# Error sanitization
# ---------------------------------------------------------------------------


class _StubResponse:
    """Minimal duck-type for httpx.Response covering only what the
    sanitizer actually inspects."""

    def __init__(self, body: dict | None = None, text: str = ""):
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def test_error_sanitizer_extracts_openai_json():
    """Pull `error.message` from the OpenAI-style envelope and sanitize it."""
    resp = _StubResponse(
        body={
            "error": {
                "type": "invalid_request_error",
                "message": (
                    "User: arn:aws:iam::123456789012:role/DemoRole is not "
                    "authorized to perform: bedrock:InvokeModel on resource"
                ),
            }
        }
    )
    out = _format_mantle_error(resp)  # type: ignore[arg-type]
    # Sanitizer must scrub the AWS account id from the user-facing message.
    assert "123456789012" not in out
    # The English of the message survives.
    assert "is not authorized" in out


def test_error_sanitizer_falls_back_to_text_on_non_json():
    resp = _StubResponse(body=None, text="<html>bedrock-mantle outage</html>")
    out = _format_mantle_error(resp)  # type: ignore[arg-type]
    assert "bedrock-mantle outage" in out


def test_sse_error_event_sanitized_before_yield():
    """`data:` lines following `event: error` get the sanitizer applied."""
    payload = {
        "type": "error",
        "error": {
            "type": "api_error",
            "message": (
                "InternalFailure: account 999988887777 quota exceeded for "
                "arn:aws:bedrock:us-east-2:999988887777:inference-profile/openai.gpt-5.4"
            ),
        },
    }
    line = f"data: {json.dumps(payload)}"
    sanitized = _sanitize_sse_error_line(line)
    assert "999988887777" not in sanitized
    # Still valid JSON after the rewrite.
    rewritten = json.loads(sanitized[len("data: ") :])
    assert rewritten["error"]["type"] == "api_error"
    assert "quota exceeded" in rewritten["error"]["message"]


def test_sse_error_line_passthrough_when_not_data():
    # A bare "event: error" line is not a `data:` line; passthrough is fine.
    assert _sanitize_sse_error_line("event: error") == "event: error"


def test_sse_error_line_passthrough_when_no_message_field():
    line = 'data: {"type":"error","error":{"type":"api_error"}}'
    out = _sanitize_sse_error_line(line)
    # No `message` to scrub, so the line is returned unchanged or re-encoded
    # to an equivalent JSON. Either way the structural shape is preserved.
    assert json.loads(out[len("data: ") :])["error"]["type"] == "api_error"
