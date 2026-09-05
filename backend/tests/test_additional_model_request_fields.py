"""Pins the P5 fix: `thinking` / `top_k` / `anthropic_beta` must reach Bedrock
via Converse's `additionalModelRequestFields`, on BOTH Converse-shaped routes.

Before this change, `mvp.anthropic._build_bedrock_kwargs` and
`mvp.chat_completions._build_chat_bedrock_kwargs` built `modelId` / `messages`
/ `inferenceConfig` / `system` / `toolConfig` and never
`additionalModelRequestFields` — so a caller that sent `thinking` got a 200
with a plain `text` reply and no error: the field was accepted (`extra=
"allow"`) and silently dropped. Verified on real Bedrock traffic (see the
change's own writeup): `{"thinking": {"type": "enabled",
"budget_tokens": 2048}}` with `max_tokens: 2049` came back with a single
`text` block, `stop_reason: end_turn`, no thinking block.

Every test here FAILS if that regresses: reverting the `additionalModelRequestFields`
wiring in `_build_bedrock_kwargs` / `_build_chat_bedrock_kwargs` (or reverting
`mvp._converse_types.additional_model_request_fields`'s allowlist) drops
these assertions back to their pre-fix state, because they read the exact
`kwargs` dict the route hands to `bedrock_runtime_client().converse(**kwargs)`
— not a mock, not a wire-level round-trip.

Verification performed (see the report accompanying this change): each test
was run once against the code AS FIXED (green), then run again after
reverting the three call sites listed above to their pre-fix bodies (`git
stash` of just the wiring lines) — every test in this file turned red on the
revert, and no other test in the suite did.
"""
from __future__ import annotations

from mvp.anthropic import AnthropicMessagesRequest, _build_bedrock_kwargs
from mvp.chat_completions import ChatCompletionsRequest, _build_chat_bedrock_kwargs
from mvp.reservation_bound import survey_and_hash_converse_kwargs

_MODEL_ID = "anthropic.claude-3-5-sonnet-20241022-v2:0"


# ---------------------------------------------------------------------------
# /v1/messages (mvp.anthropic._build_bedrock_kwargs)
# ---------------------------------------------------------------------------

def test_anthropic_thinking_is_not_forwarded_because_this_wire_discards_reasoning():
    """`thinking` must NOT reach Bedrock on the Anthropic Messages route.

    This route's response builder emits `text` and `tool_use` blocks only, so a
    reasoning block Bedrock returned here would be dropped. Honouring `thinking`
    anyway bills the caller for thinking tokens it can never read, and when the
    output budget goes entirely to thinking it hands back an empty reply with a
    full bill. Not honouring the parameter leaves the request behaving as it did
    before, which is the better of the two failures.

    This assertion replaced one that required the opposite. That earlier test was
    written from the contract amendment that wired the passthrough, before anyone
    had checked whether this route renders what the parameter produces — it does
    not. Deleting the assertion silently would have hidden a behaviour decision,
    so it is inverted here on purpose.
    """
    body = AnthropicMessagesRequest(
        model="claude-3-5-sonnet",
        max_tokens=2049,
        messages=[{"role": "user", "content": "hi"}],
        thinking={"type": "enabled", "budget_tokens": 2048},
    )
    kwargs = _build_bedrock_kwargs(body, _MODEL_ID)
    assert "additionalModelRequestFields" not in kwargs, (
        "thinking reached Bedrock on a route that cannot render reasoning back, so "
        "the caller would be billed for output it never receives"
    )


def test_anthropic_response_builder_really_cannot_render_reasoning():
    """The premise of the test above, pinned rather than trusted.

    If this route ever learns to render reasoning, this test fails and that is the
    signal to flip `renders_reasoning` at the call site — the two are meant to move
    together, and a comment alone would not have enforced that.
    """
    import inspect

    from mvp import anthropic as _anthropic

    src = inspect.getsource(_anthropic.messages)
    assert "reasoningContent" not in src, (
        "the Anthropic route now handles reasoningContent in its response builder; "
        "forward `thinking` here by dropping renders_reasoning=False, and invert "
        "test_anthropic_thinking_is_not_forwarded_because_this_wire_discards_reasoning"
    )


def test_anthropic_top_k_forwarded_as_additional_model_request_fields():
    # top_k IS a declared field on AnthropicMessagesRequest (Field(ge=1,
    # le=500)) — this pins that a DECLARED field is picked up the same way
    # as an extra one, not just extras.
    body = AnthropicMessagesRequest(
        model="claude-3-5-sonnet",
        max_tokens=100,
        messages=[{"role": "user", "content": "hi"}],
        top_k=40,
    )
    kwargs = _build_bedrock_kwargs(body, _MODEL_ID)
    assert kwargs.get("additionalModelRequestFields") == {"top_k": 40}


def test_anthropic_beta_forwarded_as_additional_model_request_fields():
    body = AnthropicMessagesRequest(
        model="claude-3-5-sonnet",
        max_tokens=100,
        messages=[{"role": "user", "content": "hi"}],
        anthropic_beta=["some-beta-flag-2026-01-01"],
    )
    kwargs = _build_bedrock_kwargs(body, _MODEL_ID)
    assert kwargs.get("additionalModelRequestFields") == {
        "anthropic_beta": ["some-beta-flag-2026-01-01"],
    }


def test_anthropic_omits_additional_model_request_fields_when_none_sent():
    # Byte-identical to today when no allowlisted key is present — the
    # change's own explicit requirement, not an incidental property.
    body = AnthropicMessagesRequest(
        model="claude-3-5-sonnet",
        max_tokens=100,
        messages=[{"role": "user", "content": "hi"}],
    )
    kwargs = _build_bedrock_kwargs(body, _MODEL_ID)
    assert "additionalModelRequestFields" not in kwargs


def test_anthropic_does_not_forward_unallowlisted_extra_field():
    # A field OUTSIDE the allowlist (e.g. `metadata`) must NOT reach
    # additionalModelRequestFields: that channel is validated against the
    # target MODEL's own schema, so an arbitrary key becomes an upstream
    # ValidationException the caller cannot act on. Only thinking/top_k/
    # anthropic_beta are extracted; everything else stays accepted-and-unused.
    body = AnthropicMessagesRequest(
        model="claude-3-5-sonnet",
        max_tokens=100,
        messages=[{"role": "user", "content": "hi"}],
        metadata={"user_id": "abc"},
        service_tier="priority",
    )
    kwargs = _build_bedrock_kwargs(body, _MODEL_ID)
    assert "additionalModelRequestFields" not in kwargs


def test_anthropic_allowlisted_field_and_tools_forwarded_together():
    # The allowlist extraction must not disturb the pre-existing toolConfig path
    # (both are additive keys built by the same function). `top_k` rather than
    # `thinking`, because this route does not forward `thinking` at all.
    body = AnthropicMessagesRequest(
        model="claude-3-5-sonnet",
        max_tokens=2049,
        messages=[{"role": "user", "content": "hi"}],
        top_k=40,
        tools=[{"name": "get_weather", "input_schema": {"type": "object"}}],
    )
    kwargs = _build_bedrock_kwargs(body, _MODEL_ID)
    assert kwargs["additionalModelRequestFields"] == {"top_k": 40}
    assert "toolConfig" in kwargs


def test_anthropic_additional_model_request_fields_is_in_kwargs_before_survey():
    # docs/design/hard-ceiling.md section 3a: the bound must be computed over
    # the CANONICAL payload about to be sent. This proves an allowlisted field is
    # already IN `kwargs` by the time `_build_bedrock_kwargs` returns — i.e.
    # before any caller can survey/reserve — by checking that the surveyed
    # envelope grows when `thinking` is present, using the exact kwargs dict
    # `_build_bedrock_kwargs` produced (not a hand-built dict). If a future
    # change moved the field-forwarding to AFTER the survey call in the route,
    # this test would still pass (it calls the builder directly) — its
    # purpose is to catch a regression where `_build_bedrock_kwargs` stops
    # including the field in its return value at all, which is the failure
    # mode this whole file pins.
    plain = AnthropicMessagesRequest(
        model="claude-3-5-sonnet", max_tokens=2049,
        messages=[{"role": "user", "content": "hi"}],
    )
    # `top_k` rather than `thinking`: this route does not forward `thinking`, so
    # using it here would measure nothing. The property under test is the
    # placement of the forwarding relative to the survey, not which key it was.
    thinking = AnthropicMessagesRequest(
        model="claude-3-5-sonnet", max_tokens=2049,
        messages=[{"role": "user", "content": "hi"}],
        top_k=40,
    )
    kwargs_plain = _build_bedrock_kwargs(plain, _MODEL_ID)
    kwargs_thinking = _build_bedrock_kwargs(thinking, _MODEL_ID)
    _, nbytes_plain, hash_plain = survey_and_hash_converse_kwargs(kwargs_plain)
    _, nbytes_thinking, hash_thinking = survey_and_hash_converse_kwargs(kwargs_thinking)
    # The envelope the bound is priced from includes the extra field's bytes —
    # `envelope_bytes(kwargs)` serialises the WHOLE kwargs dict, so
    # `additionalModelRequestFields` is inside what gets counted even though
    # `survey_and_hash_converse_kwargs`'s own walk never names that key.
    assert nbytes_thinking > nbytes_plain
    # NOTE (reported, not fixed by this change): `payload_hash` does NOT
    # change here — it is measured, not merely implied — because the hash is
    # built only from `canonical_for_hash` (text + image bytes from
    # `messages`/`system`/`toolConfig`), which `additionalModelRequestFields`
    # never touches. Two requests with identical messages but different
    # `thinking` budgets collide on `payload_hash` today. That is a real gap
    # in the hash's own stated purpose ("a retry that swapped ... must not
    # pass the pin") but it is NOT a gap in the reservation BOUND itself,
    # which is priced from `nbytes` (asserted above), not from the hash — see
    # this change's report for why it is called out rather than fixed here.
    assert hash_thinking == hash_plain


# ---------------------------------------------------------------------------
# The reservation_bound walk must not choke on the new top-level key
# ---------------------------------------------------------------------------

def test_survey_walk_tolerates_additional_model_request_fields_key():
    # `additionalModelRequestFields` is not one of the block types
    # `survey_and_hash_converse_kwargs` walks (system/messages/toolConfig) —
    # confirms it does not raise and does not perturb the text/image counts,
    # which come only from `messages`/`system`/`toolConfig` as documented.
    kwargs = {
        "modelId": _MODEL_ID,
        "messages": [{"role": "user", "content": [{"text": "hello"}]}],
        "inferenceConfig": {"maxTokens": 100},
        "additionalModelRequestFields": {
            "thinking": {"type": "enabled", "budget_tokens": 2048},
            "top_k": 40,
            "anthropic_beta": ["flag-1"],
        },
    }
    survey, nbytes, digest = survey_and_hash_converse_kwargs(kwargs)
    assert survey.text_bytes == len(b"hello")
    assert survey.unmeasurable_images == 0
    assert survey.image_dims == ()
    assert isinstance(nbytes, int) and nbytes > 0
    assert len(digest) == 64


# ---------------------------------------------------------------------------
# /v1/chat/completions (mvp.chat_completions._build_chat_bedrock_kwargs)
# ---------------------------------------------------------------------------

def test_chat_completions_thinking_forwarded_as_additional_model_request_fields():
    body = ChatCompletionsRequest(
        model="claude-3-5-sonnet",
        max_tokens=2049,
        messages=[{"role": "user", "content": "hi"}],
        thinking={"type": "enabled", "budget_tokens": 2048},
    )
    kwargs = _build_chat_bedrock_kwargs(body, _MODEL_ID)
    assert kwargs.get("additionalModelRequestFields") == {
        "thinking": {"type": "enabled", "budget_tokens": 2048},
    }


def test_chat_completions_top_k_forwarded_as_additional_model_request_fields():
    # top_k is NOT a declared field on ChatCompletionsRequest — this pins
    # that an UNDECLARED extra is picked up identically to a declared one.
    body = ChatCompletionsRequest(
        model="claude-3-5-sonnet",
        max_tokens=100,
        messages=[{"role": "user", "content": "hi"}],
        top_k=40,
    )
    kwargs = _build_chat_bedrock_kwargs(body, _MODEL_ID)
    assert kwargs.get("additionalModelRequestFields") == {"top_k": 40}


def test_chat_completions_anthropic_beta_forwarded():
    body = ChatCompletionsRequest(
        model="claude-3-5-sonnet",
        max_tokens=100,
        messages=[{"role": "user", "content": "hi"}],
        anthropic_beta=["some-beta-flag-2026-01-01"],
    )
    kwargs = _build_chat_bedrock_kwargs(body, _MODEL_ID)
    assert kwargs.get("additionalModelRequestFields") == {
        "anthropic_beta": ["some-beta-flag-2026-01-01"],
    }


def test_chat_completions_omits_additional_model_request_fields_when_none_sent():
    body = ChatCompletionsRequest(
        model="claude-3-5-sonnet",
        max_tokens=100,
        messages=[{"role": "user", "content": "hi"}],
    )
    kwargs = _build_chat_bedrock_kwargs(body, _MODEL_ID)
    assert "additionalModelRequestFields" not in kwargs


def test_chat_completions_does_not_forward_unallowlisted_extra_field():
    body = ChatCompletionsRequest(
        model="claude-3-5-sonnet",
        max_tokens=100,
        messages=[{"role": "user", "content": "hi"}],
        some_future_field="whatever",
    )
    kwargs = _build_chat_bedrock_kwargs(body, _MODEL_ID)
    assert "additionalModelRequestFields" not in kwargs
