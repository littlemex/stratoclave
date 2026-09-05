"""A field this gateway says it forwards must actually reach the provider.

The defect this closes: `thinking` was accepted by `AnthropicMessagesRequest`, named in
that model's own comment among fields "we forward to Bedrock without needing to
understand", and never sent — nothing built `additionalModelRequestFields` at all. A
request enabling extended thinking was accepted and silently dropped, which is what clause
C13.1's section intro forbids: a parameter this gateway cannot honour is refused, not
dropped.

A list of forwarded fields would not have caught it. `thinking` WAS on such a list, in
prose, and prose does not execute. What catches it is treating `FIELD_DISPOSITION` as a
claim per field and running it: build the payload and look at what is in it. The classes
are checked in both directions, so widening what reaches the model requires moving a field
between them rather than editing one call site.

Deliberately NOT asserted: that `FIELD_DISPOSITION` enumerates every field Anthropic will
ever ship. `extra="allow"` exists precisely so a new one does not 422, and a test demanding
completeness against an evolving upstream would fail on the upstream's schedule rather than
on a defect here. The set under test is what this gateway claims to handle, because that is
the set a claim can be false about.
"""
from __future__ import annotations

import pytest

from mvp._converse_types import (
    ACCEPTED_AND_UNUSED_FIELDS,
    ADDITIONAL_MODEL_REQUEST_FIELD_KEYS,
    FIELD_DISPOSITION,
    FORWARDED_FIELDS,
    RENDERED_ONLY_FIELD_KEYS,
)
from mvp.anthropic import AnthropicMessagesRequest, _build_bedrock_kwargs

_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

#: A value of the right shape per field, so the builder has something real to carry.
_SAMPLE = {
    "thinking": {"type": "enabled", "budget_tokens": 1024},
    "top_k": 40,
    "anthropic_beta": ["computer-use-2024-10-22"],
    "metadata": {"user_id": "u1"},
    "service_tier": "standard",
}


def _kwargs_with(field: str, value: object) -> dict:
    body = AnthropicMessagesRequest(
        model="claude-3-5-sonnet",
        max_tokens=2049,
        messages=[{"role": "user", "content": "hi"}],
        **{field: value},
    )
    return _build_bedrock_kwargs(body, _MODEL_ID)


def test_every_disposition_is_one_of_the_three_classes():
    """A field with a typo'd class would otherwise be silently unchecked below."""
    unknown = {k: v for k, v in FIELD_DISPOSITION.items()
               if v not in {"forwarded", "read_by_name", "accepted_and_unused"}}
    assert not unknown, f"fields classified as something this test cannot check: {unknown}"


def test_the_allowlist_and_the_forwarded_class_are_the_same_set():
    """Two declarations of one fact drift; this fails when they do.

    `ADDITIONAL_MODEL_REQUEST_FIELD_KEYS` is what the code iterates and `FORWARDED_FIELDS`
    is what this file checks. If they disagree, one of them is unchecked.
    """
    assert set(ADDITIONAL_MODEL_REQUEST_FIELD_KEYS) == FORWARDED_FIELDS, (
        "the allowlist the code iterates and the fields classified as forwarded have "
        "diverged, so one of the two is not what this test verifies"
    )


@pytest.mark.parametrize("field", sorted(FORWARDED_FIELDS - RENDERED_ONLY_FIELD_KEYS))
def test_a_field_classified_as_forwarded_reaches_the_provider(field):
    """The assertion `thinking` would have failed.

    Fields in `RENDERED_ONLY_FIELD_KEYS` are excluded here and covered separately, because
    on this route they are deliberately not forwarded — the route cannot render what they
    produce, and billing a caller for output it cannot read is the worse failure.
    """
    kwargs = _kwargs_with(field, _SAMPLE[field])
    amrf = kwargs.get("additionalModelRequestFields") or {}
    assert field in amrf, (
        f"{field!r} is classified as forwarded but does not appear in the Converse payload. "
        f"Either send it, or reclassify it as accepted_and_unused — a field documented as "
        f"forwarded and then dropped is the defect this test exists for. "
        f"Payload keys: {sorted(kwargs)}"
    )
    assert amrf[field] == _SAMPLE[field], (
        f"{field!r} reached the provider with a value the caller did not send"
    )


@pytest.mark.parametrize("field", sorted(RENDERED_ONLY_FIELD_KEYS))
def test_a_rendered_only_field_is_withheld_where_it_cannot_be_rendered(field):
    """The other half of the same rule, on the route that cannot render the result.

    This is not "the field is broken". It is that honouring a parameter whose output the
    transport discards charges the caller for tokens it never receives, so the parameter is
    declined instead. The route that renders it forwards it; that is checked in
    `test_additional_model_request_fields.py`.
    """
    kwargs = _kwargs_with(field, _SAMPLE[field])
    amrf = kwargs.get("additionalModelRequestFields") or {}
    assert field not in amrf, (
        f"{field!r} reached the provider on the Anthropic Messages route, which renders "
        f"text and tool_use blocks only. The caller would pay for output it cannot read."
    )


@pytest.mark.parametrize("field", sorted(ACCEPTED_AND_UNUSED_FIELDS))
def test_a_field_classified_as_unused_does_not_reach_the_provider(field):
    """The class checked in the other direction.

    `extra="allow"` exists so a new field does not 422, not so every field this gateway does
    not understand silently reaches the model anyway. A change that starts forwarding one of
    these has to reclassify it first, which is where a reviewer gets to see the decision.
    """
    kwargs = _kwargs_with(field, _SAMPLE[field])
    amrf = kwargs.get("additionalModelRequestFields") or {}
    assert field not in amrf, (
        f"{field!r} is classified as accepted and unused but now reaches the provider. "
        f"If that is intended, move it to the forwarded class so it is checked as one."
    )
    assert field not in kwargs, (
        f"{field!r} reached the Converse payload as a top-level key, which Bedrock does "
        f"not define; it would be an upstream ValidationException the caller cannot act on"
    )


def test_a_request_carrying_no_classified_field_is_unchanged():
    """The floor: none of this may alter a request that uses none of it.

    Without this, every assertion above could be satisfied by a builder that always emits
    `additionalModelRequestFields`, which would change the payload — and therefore the
    reservation's byte count — for every existing caller.
    """
    body = AnthropicMessagesRequest(
        model="claude-3-5-sonnet",
        max_tokens=2049,
        messages=[{"role": "user", "content": "hi"}],
    )
    kwargs = _build_bedrock_kwargs(body, _MODEL_ID)
    assert "additionalModelRequestFields" not in kwargs, (
        "a request sending none of the classified fields still carries the key, so the "
        "payload every existing caller sends has changed"
    )


def test_the_payload_hash_distinguishes_two_requests_that_differ_only_in_thinking():
    """Two requests with the same messages and different thinking budgets are not
    the same request, and the pin must say so.

    `payload_hash` claims to cover "the ENTIRE canonical payload"; it was built from
    the text and image bytes of `messages`, `system` and `toolConfig` only, so a
    request whose only difference was its thinking budget hashed identically. That
    matters because the two produce different output and a different charge, which
    is exactly what a pin recorded next to the money is for.

    The reservation BOUND was never affected — `envelope_bytes(kwargs)` serialises
    the whole dict and already covered the field — so this was a gap in the hash's
    own stated purpose rather than an underpricing.
    """
    from mvp.chat_completions import ChatCompletionsRequest, _build_chat_bedrock_kwargs
    from mvp.reservation_bound import survey_and_hash_converse_kwargs

    def _hash(budget: int | None) -> str:
        extra = {"thinking": {"type": "enabled", "budget_tokens": budget}} if budget else {}
        body = ChatCompletionsRequest(
            model="claude-sonnet-4-5",
            max_tokens=8192,
            messages=[{"role": "user", "content": "the same prompt in every case"}],
            **extra,
        )
        kwargs = _build_chat_bedrock_kwargs(body, _MODEL_ID)
        return survey_and_hash_converse_kwargs(kwargs)[2]

    plain, small, large = _hash(None), _hash(1024), _hash(4096)
    assert small != large, (
        "two requests differing only in thinking budget hash identically, so the pin "
        "recorded beside the charge cannot tell them apart"
    )
    assert plain not in (small, large), (
        "a request with no thinking config hashes the same as one with it"
    )
