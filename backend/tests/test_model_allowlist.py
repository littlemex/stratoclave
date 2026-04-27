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
