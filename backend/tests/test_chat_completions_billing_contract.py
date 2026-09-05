"""Phase-3 P1 contract tests for `/v1/chat/completions` (Converse-backed
`wire_protocol="messages"` models only — the OpenAI-compatible pass-through
transport is untouched by this contract).

Pins, from `router-requests/03-impl/HANDOFF.md`'s interface section, the
in-scope entries C8.1 (absent usage never settles as zero), C8.4 (the
reported usage block's legs sum to what was handed to settle, cache legs
never a zero stand-in for absence), C13.4 (reasoning legs render identically
on both transports), and C13.1 (`stream_options`, the terminal usage chunk
latch, and error/`[DONE]` framing).

None of this is implemented at `bb0fb2c` (base commit for phase 3) —
`ChatCompletionsRequest` has no `stream_options` field, the streaming
`_ChatAdapter` never renders a `Usage` event, and the non-streaming path
settles `int(usage.get(..., 0))` unconditionally. Every test here is expected
to fail red against that base; see the test-author report for which ones do
and why.
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
    Bedrock Converse. Mirrors `test_e2e_api_streaming.py`'s `api_client`
    fixture; kept local (rather than imported) so this file's Bedrock stub can
    vary per test without fighting a shared one.

    `_PERMS_CACHE` entries are set through `monkeypatch.setitem` rather than a
    bare assignment: a bare assignment leaks a long-TTL, hardcoded permission
    list for the "admin"/"user" roles into every OTHER test file that runs
    afterward in the same process (module-global, no per-test reset) — this is
    the exact pollution that made `test_contract_authority_source.py`'s
    `test_a_group_claim_in_the_token_grants_nothing` fail when this file's
    tests ran before it in a full-suite run. `monkeypatch.setitem` restores the
    prior entry (or removes the key if it was absent) on teardown.
    """
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
    """Parse `data:` lines into (dict for JSON, or the literal "[DONE]")."""
    out: list = []
    for line in resp.text.split("\n"):
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload:
            continue
        if payload == "[DONE]":
            out.append("[DONE]")
        else:
            out.append(json.loads(payload))
    return out


# ---------------------------------------------------------------------------
# C8.1 — non-streaming: a response with no usage key must not settle.
# ---------------------------------------------------------------------------


def test_nonstreaming_response_with_no_usage_key_does_not_settle(api_client, monkeypatch):
    """Today, `chat_completions`'s non-streaming branch does
    `usage = resp.get("usage", {})` then `int(usage.get("inputTokens", 0))`
    unconditionally and ALWAYS calls `hold.claim_settle(...)` — so a Bedrock
    response with no `usage` key at all settles at (0, 0) and is recorded as
    fully observed. `mvp.chat_completions._settle_reservation_and_log` is the
    module's own documented test seam (see `Hold`'s docstring: "a suite that
    patches `mvp.chat_completions._settle_reservation_and_log` still observes
    the write") — it must NOT be called when the provider's response carried
    no usage block.
    """
    client, mock_bedrock = api_client
    mock_bedrock.return_value.converse.return_value = {
        "output": {"message": {"content": [{"text": "hi"}]}},
        "stopReason": "end_turn",
        # No "usage" key at all.
    }

    calls = {"n": 0}
    import mvp.chat_completions as cc

    real_settle = cc._settle_reservation_and_log

    def _counting_settle(**kwargs):
        calls["n"] += 1
        return real_settle(**kwargs)

    monkeypatch.setattr(cc, "_settle_reservation_and_log", _counting_settle)

    _post(client, stream=False)

    assert calls["n"] == 0, (
        "settle was invoked for a response with no usage key — this is the "
        "exact zero-settle-as-observed corruption C8.1 pins"
    )


def test_nonstreaming_response_with_partial_usage_does_not_settle(api_client, monkeypatch):
    """Same defect, narrower trigger: `usage` present but missing
    `outputTokens` (the shape the contract's own metadata-branch fixture
    uses). `int(usage.get("outputTokens", 0))` silently reads a 0 for a field
    the provider never sent."""
    client, mock_bedrock = api_client
    mock_bedrock.return_value.converse.return_value = {
        "output": {"message": {"content": [{"text": "hi"}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 12},
    }

    calls = {"n": 0}
    import mvp.chat_completions as cc

    real_settle = cc._settle_reservation_and_log

    def _counting_settle(**kwargs):
        calls["n"] += 1
        return real_settle(**kwargs)

    monkeypatch.setattr(cc, "_settle_reservation_and_log", _counting_settle)

    _post(client, stream=False)

    assert calls["n"] == 0


# ---------------------------------------------------------------------------
# C8.4 — the reported usage block's legs sum to what settle received; an
# absent leg is never a zero stand-in; total_tokens keeps excluding the
# cache legs.
# ---------------------------------------------------------------------------

# Real measured fixture (see router-requests/03-impl/HANDOFF.md): two converse calls over one
# 3,524-token cached prefix. Call 1 WRITES the cache; every leg is explicitly
# reported by Bedrock (cacheReadInputTokens is a reported 0, not absent).
_CACHE_WRITE_USAGE = {
    "inputTokens": 10, "outputTokens": 4,
    "cacheWriteInputTokens": 3524, "cacheReadInputTokens": 0,
    "totalTokens": 3538,
}
# Call 2 READS the cache.
_CACHE_READ_USAGE = {
    "inputTokens": 10, "outputTokens": 4,
    "cacheWriteInputTokens": 0, "cacheReadInputTokens": 3524,
    "totalTokens": 3538,
}


def _reported_leg_sum(usage_block: dict) -> int:
    """Sum of the four legs the CALLER was told about, treating an omitted
    or null cache leg as 0 for the purpose of the sum (never treating a
    present 0 differently from an omitted key — that distinction is checked
    separately)."""
    return (
        int(usage_block.get("prompt_tokens") or 0)
        + int(usage_block.get("completion_tokens") or 0)
        + int(usage_block.get("cache_read_input_tokens") or 0)
        + int(usage_block.get("cache_creation_input_tokens") or 0)
    )


def test_nonstreaming_reported_legs_sum_to_the_legs_handed_to_settle(api_client):
    """The artifact's reason for existing: on `origin/main` the reported
    `total_tokens` is 14 while the call was billed 3,538 tokens. The NEW cache
    keys must appear and their sum, together with prompt/completion, must
    equal what was billed — not what `total_tokens` says."""
    client, mock_bedrock = api_client
    mock_bedrock.return_value.converse.return_value = {
        "output": {"message": {"content": [{"text": "hi"}]}},
        "stopReason": "end_turn",
        "usage": _CACHE_WRITE_USAGE,
    }

    resp = _post(client, stream=False)
    assert resp.status_code == 200
    usage = resp.json()["usage"]

    # The reported zero: cacheReadInputTokens WAS explicitly sent as 0, so it
    # must appear as a reported 0 (this is a real measurement, not an absence).
    assert usage.get("cache_read_input_tokens") == 0
    assert usage.get("cache_creation_input_tokens") == 3524

    billed_sum = 10 + 4 + 0 + 3524  # what was handed to claim_settle
    assert _reported_leg_sum(usage) == billed_sum == 3538, (
        f"reported usage {usage!r} sums to {_reported_leg_sum(usage)}, "
        f"billed {billed_sum} — a caller pricing off this block would be wrong"
    )


def test_nonstreaming_total_tokens_still_excludes_cache_legs(api_client):
    """`total_tokens` keeps TODAY's formula (`prompt_tokens + completion_tokens`)
    and must stay 14 on the real fixture even though 3,538 tokens were billed
    — pinned so a well-meaning "fix" that folds the cache legs into
    total_tokens is caught (the docs must say it excludes them, and the
    number must match the docs)."""
    client, mock_bedrock = api_client
    mock_bedrock.return_value.converse.return_value = {
        "output": {"message": {"content": [{"text": "hi"}]}},
        "stopReason": "end_turn",
        "usage": _CACHE_WRITE_USAGE,
    }

    resp = _post(client, stream=False)
    usage = resp.json()["usage"]
    assert usage["prompt_tokens"] == 10
    assert usage["completion_tokens"] == 4
    assert usage["total_tokens"] == 14, (
        "total_tokens must keep excluding the cache legs, not silently grow to "
        "include them"
    )


def test_nonstreaming_absent_cache_leg_is_null_never_zero(api_client):
    """The trap: an implementation that reports zeros for an absent leg passes
    a weaker test. Here Bedrock's usage block carries NEITHER cache key at
    all (a model that never reports prompt-cache activity) — the response
    must show null/omitted, never 0, for both cache fields."""
    client, mock_bedrock = api_client
    mock_bedrock.return_value.converse.return_value = {
        "output": {"message": {"content": [{"text": "hi"}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 10, "outputTokens": 4},
    }

    resp = _post(client, stream=False)
    usage = resp.json()["usage"]
    assert usage.get("cache_read_input_tokens") is None, (
        f"expected null/omitted for an unreported leg, got {usage.get('cache_read_input_tokens')!r} "
        "— an implementation reporting zeros here must FAIL this test"
    )
    assert usage.get("cache_creation_input_tokens") is None, (
        f"expected null/omitted for an unreported leg, got {usage.get('cache_creation_input_tokens')!r} "
        "— an implementation reporting zeros here must FAIL this test"
    )


def test_nonstreaming_cache_read_leg_reported_symmetrically_to_cache_write(api_client):
    """The other half of the real measured pair: call 2 READS the same
    3,524-token prefix instead of writing it. The two legs are disjoint and
    independently reported — a fix that only wired up cache_write (because
    the write fixture was tested first) must not silently leave read unwired."""
    client, mock_bedrock = api_client
    mock_bedrock.return_value.converse.return_value = {
        "output": {"message": {"content": [{"text": "hi"}]}},
        "stopReason": "end_turn",
        "usage": _CACHE_READ_USAGE,
    }

    resp = _post(client, stream=False)
    usage = resp.json()["usage"]
    assert usage.get("cache_read_input_tokens") == 3524
    assert usage.get("cache_creation_input_tokens") == 0
    billed_sum = 10 + 4 + 3524 + 0
    assert _reported_leg_sum(usage) == billed_sum == 3538


def test_streaming_reported_legs_sum_to_the_legs_handed_to_settle(api_client):
    """Same property, streaming transport, `stream_options.include_usage`
    requested. The terminal usage-only chunk's `usage` block must sum to the
    same billed total as the non-streaming case above, on the same fixture."""
    client, mock_bedrock = api_client
    mock_bedrock.return_value.converse_stream.return_value = {"stream": iter([
        {"contentBlockDelta": {"delta": {"text": "hi"}}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": _CACHE_WRITE_USAGE}},
    ])}

    resp = _post(client, stream=True, extra={"stream_options": {"include_usage": True}})
    assert resp.status_code == 200
    chunks = _sse_chunks(resp)
    usage_chunks = [c for c in chunks if isinstance(c, dict) and c.get("usage")]
    assert len(usage_chunks) == 1, f"expected exactly one usage-bearing chunk, got {usage_chunks!r}"
    usage = usage_chunks[0]["usage"]

    assert usage.get("cache_read_input_tokens") == 0
    assert usage.get("cache_creation_input_tokens") == 3524
    billed_sum = 10 + 4 + 0 + 3524
    assert _reported_leg_sum(usage) == billed_sum == 3538


# ---------------------------------------------------------------------------
# C13.4 (parity) — one reasoning block's content, fed through both the
# non-streaming and streaming paths, yields the same legs.
# ---------------------------------------------------------------------------


def test_reasoning_content_parity_across_streaming_and_nonstreaming(api_client):
    """A block carrying text="the reasoning" and signature="sig-parity",
    expressed the way EACH transport actually sees it (a single nested
    reasoningContent/reasoningText block non-streaming; a sequence of flat
    reasoningContent deltas streaming) must render to the SAME
    reasoning_content legs on both transports. This is the check that a
    leg added to one path and not the other cannot pass silently.
    """
    client, mock_bedrock = api_client

    mock_bedrock.return_value.converse.return_value = {
        "output": {"message": {"content": [
            {"reasoningContent": {"reasoningText": {
                "text": "the reasoning", "signature": "sig-parity",
            }}},
            {"text": "the answer"},
        ]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 5, "outputTokens": 5},
    }
    nonstream_resp = _post(client, stream=False)
    assert nonstream_resp.status_code == 200
    nonstream_message = nonstream_resp.json()["choices"][0]["message"]
    nonstream_reasoning = nonstream_message.get("reasoning_content") or {}

    mock_bedrock.return_value.converse_stream.return_value = {"stream": iter([
        {"contentBlockDelta": {"delta": {"reasoningContent": {"text": "the reasoning"}}}},
        {"contentBlockDelta": {"delta": {"reasoningContent": {"signature": "sig-parity"}}}},
        {"contentBlockDelta": {"delta": {"text": "the answer"}}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 5, "outputTokens": 5}}},
    ])}
    stream_resp = _post(client, stream=True)
    assert stream_resp.status_code == 200
    chunks = _sse_chunks(stream_resp)
    stream_reasoning: dict = {}
    for c in chunks:
        if not isinstance(c, dict):
            continue
        delta = c.get("choices", [{}])[0].get("delta", {})
        rc = delta.get("reasoning_content")
        if rc:
            stream_reasoning.update(rc)

    assert nonstream_reasoning.get("text") == "the reasoning"
    assert nonstream_reasoning.get("signature") == "sig-parity"
    assert stream_reasoning.get("text") == "the reasoning"
    assert stream_reasoning.get("signature") == "sig-parity"
    assert nonstream_reasoning.get("text") == stream_reasoning.get("text")
    assert nonstream_reasoning.get("signature") == stream_reasoning.get("signature")


# ---------------------------------------------------------------------------
# C13.1 — stream_options declared + honoured, the terminal usage chunk latch.
# ---------------------------------------------------------------------------


def test_stream_false_with_stream_options_is_400(api_client):
    """`stream_options` is meaningless without `stream=true`; today it is
    silently swallowed by `extra="allow"` (the request model has no such
    field) and the request succeeds."""
    client, _ = api_client
    resp = _post(client, stream=False, extra={"stream_options": {"include_usage": True}})
    assert resp.status_code == 400


def test_streaming_without_stream_options_emits_no_usage_chunk(api_client):
    """Caller did not opt in: no usage-bearing chunk should appear at all,
    matching OpenAI's own `stream_options.include_usage` semantics (opt-in,
    not default-on) — this guards against a fix that always emits usage
    once `normalized_events` starts producing Usage events."""
    client, mock_bedrock = api_client
    mock_bedrock.return_value.converse_stream.return_value = {"stream": iter([
        {"contentBlockDelta": {"delta": {"text": "hi"}}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 5, "outputTokens": 2}}},
    ])}
    resp = _post(client, stream=True)
    chunks = _sse_chunks(resp)
    usage_chunks = [c for c in chunks if isinstance(c, dict) and c.get("usage")]
    assert usage_chunks == [], f"expected no usage chunk without include_usage, got {usage_chunks!r}"


def test_streaming_with_include_usage_nonterminal_chunks_carry_usage_null(api_client):
    client, mock_bedrock = api_client
    mock_bedrock.return_value.converse_stream.return_value = {"stream": iter([
        {"contentBlockDelta": {"delta": {"text": "hi"}}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 5, "outputTokens": 2}}},
    ])}
    resp = _post(client, stream=True, extra={"stream_options": {"include_usage": True}})
    chunks = _sse_chunks(resp)
    dict_chunks = [c for c in chunks if isinstance(c, dict)]
    nonterminal = [c for c in dict_chunks if c.get("choices")]
    assert nonterminal, "expected at least one non-terminal (content-bearing) chunk"
    for c in nonterminal:
        assert "usage" in c, f"non-terminal chunk missing the usage key entirely: {c!r}"
        assert c["usage"] is None, f"non-terminal chunk must carry usage: null, got {c['usage']!r}"

    terminal = [c for c in dict_chunks if c.get("usage")]
    assert len(terminal) == 1, f"expected exactly one terminal usage chunk, got {terminal!r}"
    assert terminal[0].get("choices") == [], "the terminal usage chunk must carry choices: []"


def test_streaming_two_metadata_events_produce_one_usage_chunk(api_client):
    """A duplicate/retried metadata event must not double the usage chunk
    (or double-count the warning the interface names alongside it)."""
    from structlog.testing import capture_logs

    client, mock_bedrock = api_client
    mock_bedrock.return_value.converse_stream.return_value = {"stream": iter([
        {"contentBlockDelta": {"delta": {"text": "hi"}}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 5, "outputTokens": 2}}},
        {"metadata": {"usage": {"inputTokens": 5, "outputTokens": 2}}},
    ])}
    with capture_logs():
        resp = _post(client, stream=True, extra={"stream_options": {"include_usage": True}})
        chunks = _sse_chunks(resp)

    usage_chunks = [c for c in chunks if isinstance(c, dict) and c.get("usage")]
    assert len(usage_chunks) == 1, (
        f"two metadata events must produce ONE usage chunk, got {len(usage_chunks)}: {usage_chunks!r}"
    )


def test_streaming_error_terminated_stream_has_error_frame_no_usage_chunk_no_done(api_client):
    """An error-terminated stream must emit its error frame and NO usage
    chunk — even with include_usage requested, there is nothing to report
    (the provider never reached a terminal usage event) — and `[DONE]` must
    NOT follow it: `[DONE]` is reserved for generator exhaustion."""

    class _BoomStream:
        def __iter__(self):
            return self

        def __next__(self):
            raise RuntimeError("bedrock stream broke mid-flight")

    client, mock_bedrock = api_client
    mock_bedrock.return_value.converse_stream.return_value = {"stream": _BoomStream()}

    resp = _post(client, stream=True, extra={"stream_options": {"include_usage": True}})
    assert resp.status_code == 200  # the error surfaces as an SSE error frame, not an HTTP error
    assert "error" in resp.text, "an error-terminated stream must emit an error frame"
    chunks = _sse_chunks(resp)
    usage_chunks = [c for c in chunks if isinstance(c, dict) and c.get("usage")]
    assert usage_chunks == [], f"an error-terminated stream must not emit a usage chunk, got {usage_chunks!r}"
    assert "[DONE]" not in chunks, "[DONE] must not follow an error-terminated stream"


def test_done_only_on_generator_exhaustion_for_a_clean_stream(api_client):
    """Positive control: a clean stream's LAST SSE data line is `[DONE]`,
    exactly once — regression guard so the error-path test above is not
    passing merely because [DONE] is never emitted at all."""
    client, mock_bedrock = api_client
    mock_bedrock.return_value.converse_stream.return_value = {"stream": iter([
        {"contentBlockDelta": {"delta": {"text": "hi"}}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 5, "outputTokens": 2}}},
    ])}
    resp = _post(client, stream=True)
    chunks = _sse_chunks(resp)
    assert chunks.count("[DONE]") == 1
    assert chunks[-1] == "[DONE]"
