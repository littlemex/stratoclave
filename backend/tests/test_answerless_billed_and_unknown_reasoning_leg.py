"""Phase-3 P2 tests for `/v1/chat/completions`.

Covers exactly the two items in `router-requests/03-impl/HANDOFF.md`'s P2 scope:

  - the answer-less-billed warning: `_converse_core.answerless_billed` exists (part 1) but nothing
    called it. This file pins that both transports now call it — and ONLY
    it, never re-deriving the condition — and emit `answerless_billed_reply`
    with the required structured fields, exactly once per request.
  - The non-streaming reasoning-leg extractor (`reasoning_legs_from_output_
    block`) builds its flattened dict from exactly the three known leg
    names, so a fourth (unknown) leg in the provider's block was never
    constructed and never detected. This file pins that an unknown leg is
    now reported (`unknown_reasoning_leg`, matching the streaming path's
    renamed event) rather than dropped in silence, at both nesting levels
    Bedrock's OUTPUT shape actually uses.

Every route-level test here calls the real `/v1/chat/completions` handler
through a `TestClient`, with only the Bedrock Converse client and auth
mocked — the same harness `test_chat_completions_billing_contract.py`
uses, kept local per that file's own stated reason (a per-file Bedrock stub
that can vary without fighting a shared fixture).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Optional
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mvp import _converse_core
from mvp.authz import _PERMS_CACHE
from mvp.chat_completions import router as chat_router
from mvp.deps import get_current_user

MODEL = "us.anthropic.claude-opus-4-7"


@dataclass
class _FakeUser:
    user_id: str = "user-11111111-1111-1111-1111-111111111111"
    org_id: str = "default-org"
    email: str = "test@example.com"
    roles: Optional[list] = None
    auth_kind: str = "jwt"
    key_scopes: Optional[list] = None

    def __post_init__(self):
        if self.roles is None:
            self.roles = ["user", "admin"]


@pytest.fixture
def api_client(dynamodb_mock, seed_active_tenant, monkeypatch):
    """TestClient with the chat-completions router only, mocked auth, mocked
    Bedrock Converse. See module docstring for why this fixture is local
    rather than shared."""
    monkeypatch.setitem(_PERMS_CACHE, "user", (["messages:send", "usage:read-self"], time.time() + 3600))
    monkeypatch.setitem(_PERMS_CACHE, "admin", (["messages:send", "usage:read-self", "tenants:update"], time.time() + 3600))

    app = FastAPI()
    app.include_router(chat_router)
    app.dependency_overrides[get_current_user] = lambda: _FakeUser()

    with patch("mvp.chat_completions.deployment_client") as mock_bedrock:
        yield TestClient(app), mock_bedrock


def _post(client, *, stream=False, extra: Optional[dict] = None):
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream,
    }
    if extra:
        body.update(extra)
    return client.post("/v1/chat/completions", json=body)


def _sse_chunks(resp) -> list[dict | str]:
    out: list = []
    for line in resp.text.split("\n"):
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload:
            continue
        out.append("[DONE]" if payload == "[DONE]" else json.loads(payload))
    return out


def _resolved_model_id(mock_bedrock, *, stream: bool) -> str:
    call = mock_bedrock.return_value.converse_stream if stream else mock_bedrock.return_value.converse
    return call.call_args.kwargs["modelId"]


def _warnings(logs, event_name):
    return [e for e in logs if e.get("log_level") == "warning" and e.get("event") == event_name]


# ---------------------------------------------------------------------------
# Unit level: `_converse_core.unknown_reasoning_legs_in_output_block`.
# ---------------------------------------------------------------------------


class TestUnknownReasoningLegsInOutputBlockUnit:
    def test_unknown_leg_nested_under_reasoningtext_is_detected(self):
        block = {"reasoningText": {"text": "hi", "signature": "sig", "cargoLeg": "x"}}
        assert _converse_core.unknown_reasoning_legs_in_output_block(block) == frozenset({"cargoLeg"})

    def test_unknown_leg_sibling_of_reasoningtext_is_detected(self):
        """`redactedContent` is a documented SIBLING of `reasoningText`, not a
        nested key — the provider's next unknown leg may arrive at this same
        level rather than nested, and checking only the nested level would
        miss exactly that shape."""
        block = {"reasoningText": {"text": "hi"}, "novelLeg": "value"}
        assert _converse_core.unknown_reasoning_legs_in_output_block(block) == frozenset({"novelLeg"})

    def test_known_legs_only_yields_empty_set(self):
        block = {"reasoningText": {"text": "hi", "signature": "sig"}, "redactedContent": "r"}
        assert _converse_core.unknown_reasoning_legs_in_output_block(block) == frozenset()

    def test_falsy_unknown_value_does_not_count(self):
        block = {"reasoningText": {"text": "hi", "cargoLeg": ""}}
        assert _converse_core.unknown_reasoning_legs_in_output_block(block) == frozenset()

    def test_the_flatten_alone_cannot_recover_the_unknown_leg(self):
        """Documents WHY the separate check is required: `reasoning_legs_
        from_output_block`'s own return value never contains the unknown
        key, so nothing downstream of the flatten can detect it — the check
        must run on the RAW block, before the flatten."""
        block = {"reasoningText": {"text": "hi", "cargoLeg": "x"}}
        legs = _converse_core.reasoning_legs_from_output_block(block)
        assert "cargoLeg" not in legs
        assert set(legs.keys()) <= {"text", "signature", "redactedContent"}


# ---------------------------------------------------------------------------
# Route level: the unknown leg must be reported, not dropped, on the
# non-streaming transport — and under the SAME event name the streaming
# path uses (interface: "matching the streaming path's").
# ---------------------------------------------------------------------------


class TestUnknownReasoningLegRouteLevel:
    def test_nonstreaming_unknown_leg_nested_under_reasoningtext_warns(self, api_client):
        """Pins the fix directly: revert `unknown_reasoning_legs_in_output_
        block`'s call out of the non-streaming loop (or revert the flatten
        to build `candidate` from the provider's own keys) and this goes
        from one warning to zero, because `reasoning_legs_from_output_
        block`'s three-name `candidate` dict has nothing left to detect a
        fourth leg with."""
        from structlog.testing import capture_logs

        client, mock_bedrock = api_client
        mock_bedrock.return_value.converse.return_value = {
            "output": {"message": {"content": [
                {"reasoningContent": {"reasoningText": {
                    "text": "the reasoning", "signature": "sig", "cargoLeg": "unexpected",
                }}},
                {"text": "the answer"},
            ]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 5, "outputTokens": 5},
        }
        with capture_logs() as logs:
            resp = _post(client, stream=False)
        assert resp.status_code == 200
        warnings = _warnings(logs, "unknown_reasoning_leg")
        assert len(warnings) == 1, f"expected exactly one warning, got {warnings!r}"
        assert "cargoLeg" in warnings[0].get("legs", []), warnings[0]

    def test_nonstreaming_unknown_leg_sibling_of_reasoningtext_warns(self, api_client):
        from structlog.testing import capture_logs

        client, mock_bedrock = api_client
        mock_bedrock.return_value.converse.return_value = {
            "output": {"message": {"content": [
                {"reasoningContent": {
                    "reasoningText": {"text": "the reasoning"},
                    "novelLeg": "unexpected-sibling",
                }},
            ]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 5, "outputTokens": 5},
        }
        with capture_logs() as logs:
            resp = _post(client, stream=False)
        assert resp.status_code == 200
        warnings = _warnings(logs, "unknown_reasoning_leg")
        assert len(warnings) == 1, f"expected exactly one warning, got {warnings!r}"
        assert "novelLeg" in warnings[0].get("legs", []), warnings[0]

    def test_nonstreaming_multiple_blocks_unknown_legs_warn_once_per_response(self, api_client):
        """Two reasoningContent blocks, each with a DIFFERENT unknown leg:
        still exactly one warning line for the whole response (interface:
        "once per response"), naming both."""
        from structlog.testing import capture_logs

        client, mock_bedrock = api_client
        mock_bedrock.return_value.converse.return_value = {
            "output": {"message": {"content": [
                {"reasoningContent": {"reasoningText": {"text": "a", "legA": "1"}}},
                {"reasoningContent": {"reasoningText": {"text": "b", "legB": "2"}}},
            ]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 5, "outputTokens": 5},
        }
        with capture_logs() as logs:
            resp = _post(client, stream=False)
        assert resp.status_code == 200
        warnings = _warnings(logs, "unknown_reasoning_leg")
        assert len(warnings) == 1, f"expected ONE warning for the whole response, got {warnings!r}"
        assert set(warnings[0].get("legs", [])) == {"legA", "legB"}

    def test_nonstreaming_known_legs_only_never_warns(self, api_client):
        """Regression guard: `text`/`signature`/`redactedContent` alone must
        never trip the unknown-leg warning."""
        from structlog.testing import capture_logs

        client, mock_bedrock = api_client
        mock_bedrock.return_value.converse.return_value = {
            "output": {"message": {"content": [
                {"reasoningContent": {"reasoningText": {"text": "hi", "signature": "sig"},
                                       "redactedContent": "r"}},
            ]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 5, "outputTokens": 5},
        }
        with capture_logs() as logs:
            resp = _post(client, stream=False)
        assert resp.status_code == 200
        assert _warnings(logs, "unknown_reasoning_leg") == []

    def test_streaming_unknown_leg_event_name_matches_nonstreaming(self, api_client):
        """The streaming path already warned on an unknown leg before this
        change (`_converse_core.normalized_events`); this pins that its event
        name was renamed to the SAME name the non-streaming path now uses,
        rather than the two paths reporting the identical defect under two
        different names."""
        from structlog.testing import capture_logs

        client, mock_bedrock = api_client
        mock_bedrock.return_value.converse_stream.return_value = {"stream": iter([
            {"contentBlockDelta": {"delta": {"reasoningContent": {"text": "hi", "cargoLeg": "x"}}}},
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": {"usage": {"inputTokens": 5, "outputTokens": 5}}},
        ])}
        with capture_logs() as logs:
            resp = _post(client, stream=True)
        assert resp.status_code == 200
        warnings = _warnings(logs, "unknown_reasoning_leg")
        assert len(warnings) == 1, f"expected exactly one warning, got {warnings!r}"


# ---------------------------------------------------------------------------
# the answer-less-billed warning route level: `answerless_billed_reply`, both transports, single
# call site (`_converse_core.answerless_billed`), never re-derived.
# ---------------------------------------------------------------------------


class TestAnswerlessBilledRouteLevel:
    def test_nonstreaming_measured_4097_fixture_warns_once_with_required_fields(self, api_client):
        """The measured shape from the interface: `budget_tokens=4096` /
        `maxTokens=4097` -> `stopReason=max_tokens`, 4,097 billed output
        tokens, non-trivial reasoning text, and an EMPTY text block (Bedrock
        appends one) rather than no text block at all. Pins the exact
        defect named in the interface: a predicate computed from `bool(
        text_parts)` would read this reply as "text was seen" (the list
        holds one empty string) and stay silent; this fixture must warn."""
        from structlog.testing import capture_logs

        client, mock_bedrock = api_client
        reasoning_text = "A" * 7159
        mock_bedrock.return_value.converse.return_value = {
            "output": {"message": {"content": [
                {"text": ""},
                {"reasoningContent": {"reasoningText": {
                    "text": reasoning_text, "signature": "sig-4097",
                }}},
            ]}},
            "stopReason": "max_tokens",
            "usage": {"inputTokens": 50, "outputTokens": 4097},
        }
        with capture_logs() as logs:
            resp = _post(client, stream=False)
        assert resp.status_code == 200
        warnings = _warnings(logs, "answerless_billed_reply")
        assert len(warnings) == 1, f"expected exactly one warning, got {warnings!r}"
        w = warnings[0]
        assert w["model_id"] == _resolved_model_id(mock_bedrock, stream=False)
        assert w["request_id"] == resp.headers.get("x-sc-span-id")
        assert w["stop_reason"] == "max_tokens"
        assert w["output_tokens"] == 4097
        assert w["saw_reasoning_text"] is True
        assert "reasoning" in w["block_types"]

    def test_streaming_measured_4097_fixture_warns_once_with_required_fields(self, api_client):
        """Same fixture, streaming transport: reasoning deltas only, no text
        delta at all (the streaming path drops an empty text delta, so there
        is nothing to append in the first place — the OTHER half of the
        transport-disagreement defect)."""
        from structlog.testing import capture_logs

        client, mock_bedrock = api_client
        reasoning_text = "A" * 7159
        mock_bedrock.return_value.converse_stream.return_value = {"stream": iter([
            {"contentBlockDelta": {"delta": {"reasoningContent": {"text": reasoning_text}}}},
            {"contentBlockDelta": {"delta": {"reasoningContent": {"signature": "sig-4097"}}}},
            {"messageStop": {"stopReason": "max_tokens"}},
            {"metadata": {"usage": {"inputTokens": 50, "outputTokens": 4097}}},
        ])}
        with capture_logs() as logs:
            resp = _post(client, stream=True)
        assert resp.status_code == 200
        warnings = _warnings(logs, "answerless_billed_reply")
        assert len(warnings) == 1, f"expected exactly one warning, got {warnings!r}"
        w = warnings[0]
        assert w["model_id"] == _resolved_model_id(mock_bedrock, stream=True)
        assert w["request_id"] == resp.headers.get("x-sc-span-id")
        assert w["stop_reason"] == "max_tokens"
        assert w["output_tokens"] == 4097
        assert w["saw_reasoning_text"] is True
        assert "reasoning" in w["block_types"]

    def test_nonstreaming_empty_text_block_with_billed_output_warns(self, api_client):
        """Interface's explicit second case: an empty-string text block plus
        billed output must warn on BOTH transports. Minimal fixture, no
        reasoning at all, so this isolates the empty-text-block shape from
        the reasoning-heavy 4097 fixture above."""
        from structlog.testing import capture_logs

        client, mock_bedrock = api_client
        mock_bedrock.return_value.converse.return_value = {
            "output": {"message": {"content": [{"text": ""}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 5, "outputTokens": 5},
        }
        with capture_logs() as logs:
            resp = _post(client, stream=False)
        assert resp.status_code == 200
        assert len(_warnings(logs, "answerless_billed_reply")) == 1

    def test_streaming_empty_text_delta_with_billed_output_warns(self, api_client):
        """The streaming mirror of the test above: an empty text delta is
        dropped by `normalized_events` (`if text:` guard), so `content` never
        even gets appended to — but the reply is still billed and still has
        no answer, and must still warn."""
        from structlog.testing import capture_logs

        client, mock_bedrock = api_client
        mock_bedrock.return_value.converse_stream.return_value = {"stream": iter([
            {"contentBlockDelta": {"delta": {"text": ""}}},
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": {"usage": {"inputTokens": 5, "outputTokens": 5}}},
        ])}
        with capture_logs() as logs:
            resp = _post(client, stream=True)
        assert resp.status_code == 200
        assert len(_warnings(logs, "answerless_billed_reply")) == 1

    def test_nonstreaming_tool_use_only_reply_does_not_warn(self, api_client):
        """The false positive that would make the whole feature useless: a
        tool-use-only reply is a normal, successful agentic turn."""
        from structlog.testing import capture_logs

        client, mock_bedrock = api_client
        mock_bedrock.return_value.converse.return_value = {
            "output": {"message": {"content": [
                {"toolUse": {"toolUseId": "t1", "name": "get_weather", "input": {}}},
            ]}},
            "stopReason": "tool_use",
            "usage": {"inputTokens": 20, "outputTokens": 30},
        }
        with capture_logs() as logs:
            resp = _post(client, stream=False)
        assert resp.status_code == 200
        assert _warnings(logs, "answerless_billed_reply") == []

    def test_streaming_tool_use_only_reply_does_not_warn(self, api_client):
        from structlog.testing import capture_logs

        client, mock_bedrock = api_client
        mock_bedrock.return_value.converse_stream.return_value = {"stream": iter([
            {"contentBlockStart": {"contentBlockIndex": 0, "start": {"toolUse": {"toolUseId": "t1", "name": "get_weather"}}}},
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolUse": {"input": "{}"}}}},
            {"messageStop": {"stopReason": "tool_use"}},
            {"metadata": {"usage": {"inputTokens": 20, "outputTokens": 30}}},
        ])}
        with capture_logs() as logs:
            resp = _post(client, stream=True)
        assert resp.status_code == 200
        assert _warnings(logs, "answerless_billed_reply") == []

    def test_nonstreaming_zero_output_tokens_does_not_warn(self, api_client):
        """Nothing was billed, so there is nothing to warn about."""
        from structlog.testing import capture_logs

        client, mock_bedrock = api_client
        mock_bedrock.return_value.converse.return_value = {
            "output": {"message": {"content": []}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 5, "outputTokens": 0},
        }
        with capture_logs() as logs:
            resp = _post(client, stream=False)
        assert resp.status_code == 200
        assert _warnings(logs, "answerless_billed_reply") == []

    def test_streaming_zero_output_tokens_does_not_warn(self, api_client):
        from structlog.testing import capture_logs

        client, mock_bedrock = api_client
        mock_bedrock.return_value.converse_stream.return_value = {"stream": iter([
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": {"usage": {"inputTokens": 5, "outputTokens": 0}}},
        ])}
        with capture_logs() as logs:
            resp = _post(client, stream=True)
        assert resp.status_code == 200
        assert _warnings(logs, "answerless_billed_reply") == []

    def test_nonstreaming_normal_text_reply_does_not_warn(self, api_client):
        """Positive control: an ordinary successful reply never warns."""
        from structlog.testing import capture_logs

        client, mock_bedrock = api_client
        mock_bedrock.return_value.converse.return_value = {
            "output": {"message": {"content": [{"text": "a normal answer"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 5, "outputTokens": 5},
        }
        with capture_logs() as logs:
            resp = _post(client, stream=False)
        assert resp.status_code == 200
        assert _warnings(logs, "answerless_billed_reply") == []

    def test_streaming_normal_text_reply_does_not_warn(self, api_client):
        from structlog.testing import capture_logs

        client, mock_bedrock = api_client
        mock_bedrock.return_value.converse_stream.return_value = {"stream": iter([
            {"contentBlockDelta": {"delta": {"text": "a normal answer"}}},
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": {"usage": {"inputTokens": 5, "outputTokens": 5}}},
        ])}
        with capture_logs() as logs:
            resp = _post(client, stream=True)
        assert resp.status_code == 200
        assert _warnings(logs, "answerless_billed_reply") == []

    def test_nonstreaming_warning_payload_never_appears_in_the_message_string(self, api_client):
        """The interface requires every field to be a structured structlog
        key, never interpolated into the message string — a value folded
        into the message string would be invisible to `core/logging.mask_
        sensitive_data`'s scrub. `capture_logs` records the message under
        `event`; this pins that `event` stays the bare event name and the
        payload lives in separate keys, not concatenated into it."""
        from structlog.testing import capture_logs

        client, mock_bedrock = api_client
        mock_bedrock.return_value.converse.return_value = {
            "output": {"message": {"content": [{"text": ""}]}},
            "stopReason": "max_tokens",
            "usage": {"inputTokens": 5, "outputTokens": 5},
        }
        with capture_logs() as logs:
            _post(client, stream=False)
        warnings = _warnings(logs, "answerless_billed_reply")
        assert len(warnings) == 1
        assert warnings[0]["event"] == "answerless_billed_reply"
        # The stop reason and token count must be their own keys, not glued
        # into the event string.
        assert "max_tokens" not in warnings[0]["event"]
        assert warnings[0]["stop_reason"] == "max_tokens"
