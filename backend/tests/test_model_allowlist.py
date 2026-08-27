"""resolve_bedrock_model() allowlist regression tests.

Guards PR #3 (P0-4): only Claude-family models should pass through.
Llama, Nova, Mistral, or any other Bedrock family must raise
ValueError so the FastAPI handler can surface a 400 invalid_model.
"""
from __future__ import annotations

import pytest

from mvp.models import DEFAULT_MODEL, resolve_bedrock_model


@pytest.mark.parametrize(
    "alias",
    [
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
        "claude-3-5-haiku-20241022",
    ],
)
def test_anthropic_aliases_resolve(alias: str) -> None:
    resolved = resolve_bedrock_model(alias)
    assert resolved.startswith(("us.", "apac.", "eu.", "global.")) or resolved.startswith(
        "anthropic."
    )
    assert "anthropic" in resolved


def test_default_returned_when_none() -> None:
    assert resolve_bedrock_model(None) == DEFAULT_MODEL
    assert resolve_bedrock_model("") == DEFAULT_MODEL


@pytest.mark.parametrize(
    "bad_model",
    [
        "amazon.nova-pro-v1:0",
        "us.meta.llama3-8b-instruct-v1:0",
        "mistral.mistral-large-2407-v1:0",
        "stability.stable-diffusion-xl-v1",
        "cohere.command-r-v1:0",
    ],
)
def test_non_anthropic_models_rejected(bad_model: str) -> None:
    with pytest.raises(ValueError) as exc:
        resolve_bedrock_model(bad_model)
    assert bad_model in str(exc.value)


def test_bedrock_id_pass_through_requires_allowlist() -> None:
    """A raw Bedrock ID is accepted only if it's in the allowlist; the
    `us.` / `apac.` / `eu.` / `global.` prefix alone is no longer enough.
    """
    # DEFAULT_MODEL is in the allowlist (it *is* a Bedrock ID).
    assert resolve_bedrock_model(DEFAULT_MODEL) == DEFAULT_MODEL

    # Made-up-but-correctly-prefixed ID is rejected.
    with pytest.raises(ValueError):
        resolve_bedrock_model("us.anthropic.claude-ghost-9000-v99:0")


class TestUnknownModelGuidance:
    """The rejection message must tell the caller what to send instead.

    Live verification (2026-08-27) hit `us.anthropic.claude-haiku-4-5` — a real
    Claude model whose only problem is that it is not a registered alias — and
    the old messages said "Only Claude family models are supported" (a
    contradiction) and pointed at an internal source file. Both are asserted
    against here so they cannot come back.
    """

    def test_full_bedrock_id_is_answered_with_the_short_alias(self):
        from mvp.models import resolve_model
        with pytest.raises(ValueError) as ei:
            resolve_model("us.anthropic.claude-haiku-4-5")
        msg = str(ei.value)
        # The contained alias is the right answer, not a lexically similar
        # but different model.
        assert "'claude-haiku-4-5'" in msg
        assert "claude-opus" not in msg

    def test_message_points_at_the_public_model_list_not_a_source_file(self):
        from mvp.models import resolve_model
        with pytest.raises(ValueError) as ei:
            resolve_model("totally-unknown-xyz")
        msg = str(ei.value)
        assert "GET /v1/models" in msg
        assert "backend/mvp" not in msg
        assert "_REGISTRY" not in msg

    def test_known_model_on_the_wrong_route_says_which_route_to_use(self):
        from mvp.models import resolve_bedrock_model
        with pytest.raises(ValueError) as ei:
            resolve_bedrock_model("openai.gpt-5.6-sol")
        msg = str(ei.value)
        assert "not served by the Anthropic Messages route" in msg
        assert "/v1/chat/completions" in msg
        # It must NOT claim the model is unknown or non-Claude-family.
        assert "not a recognised model name" not in msg

    def test_unknown_name_on_the_messages_route_does_not_contradict_itself(self):
        from mvp.models import resolve_bedrock_model
        with pytest.raises(ValueError) as ei:
            resolve_bedrock_model("us.anthropic.claude-haiku-4-5")
        msg = str(ei.value)
        assert "'claude-haiku-4-5'" in msg
        # The old wording asserted the model was not Claude family, which was
        # false for this input.
        assert "Only Claude family models are supported" not in msg


class TestSuggestionsAreFollowable:
    """A suggestion the caller cannot act on is worse than no suggestion.

    Both reviewers caught this on the first cut of the fix: the Messages route
    was suggesting an OpenAI alias and then, one sentence later, refusing it.
    """

    def test_messages_route_never_suggests_a_model_it_cannot_serve(self):
        from mvp.models import resolve_bedrock_model
        with pytest.raises(ValueError) as ei:
            resolve_bedrock_model("gpt-5.6-so")   # typo for an OpenAI alias
        msg = str(ei.value)
        assert "gpt-5" not in msg.split("is not a recognised model name")[1]

    def test_wrong_case_still_gets_the_right_suggestion(self):
        from mvp.models import resolve_model
        with pytest.raises(ValueError) as ei:
            resolve_model("CLAUDE-HAIKU-4-5")
        assert "'claude-haiku-4-5'" in str(ei.value)

    def test_only_aliases_are_suggested_never_raw_bedrock_ids(self):
        from mvp.models import resolve_model
        with pytest.raises(ValueError) as ei:
            resolve_model("us.anthropic.claude-sonnet-4-6-v1:0")
        msg = str(ei.value)
        # The sentence promises GET /v1/models, which lists aliases; suggesting
        # a raw Bedrock id would contradict it. Assert on the suggestion clause
        # only — the rejected input is echoed earlier in the same string.
        suggestion = msg.split("Did you mean", 1)[1]
        assert "'claude-sonnet-4-6'" in suggestion
        assert "us.anthropic" not in suggestion

    def test_a_very_short_alias_cannot_match_arbitrary_input(self):
        from mvp.models import _did_you_mean, _MIN_CONTAINED_ALIAS
        # Containment is gated on length, so a short alias cannot fire on junk.
        assert _MIN_CONTAINED_ALIAS >= 8
        assert _did_you_mean("junk-gpt-5.4-junk") == ""

    def test_the_echoed_name_is_bounded(self):
        from mvp.models import resolve_model, _MAX_ECHOED_NAME
        with pytest.raises(ValueError) as ei:
            resolve_model("x" * 5000)
        assert "x" * (_MAX_ECHOED_NAME + 1) not in str(ei.value)

    def test_empty_name_produces_no_suggestion_fragment(self):
        from mvp.models import _did_you_mean
        assert _did_you_mean(None) == ""
        assert _did_you_mean("") == ""
