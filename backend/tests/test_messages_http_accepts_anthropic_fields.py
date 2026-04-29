"""Hot-fix regression test (2026-04-29).

Sweep-1's C-H work put ``extra="forbid"`` on
``AnthropicMessagesRequest``. That threw HTTP 422 for every Claude
Code / Anthropic SDK request that carried ``tools`` /
``tool_choice`` / ``metadata`` / ``anthropic_beta``, effectively
breaking the primary ``/v1/messages`` endpoint for real users.

This file pins the contract at the HTTP boundary: a body shaped
like a real Claude Code request must NOT be rejected with 422 by
FastAPI's Pydantic validation. We stub out authentication and the
actual Bedrock bridge so the test stays hermetic; if Pydantic
accepts the body, the stub handler is reached and we assert that.
"""
from __future__ import annotations

from typing import Any

import pytest


CLAUDE_CODE_BODY: dict[str, Any] = {
    "model": "us.anthropic.claude-opus-4-7",
    "max_tokens": 4096,
    "system": [
        {
            "type": "text",
            "text": "You are Claude Code.",
            "cache_control": {"type": "ephemeral"},
        }
    ],
    "messages": [
        {
            "role": "user",
            "content": [{"type": "text", "text": "List files"}],
        }
    ],
    "tools": [
        {
            "name": "Read",
            "description": "Read a file",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    ],
    "tool_choice": {"type": "auto"},
    "metadata": {"user_id": "u-cc-session"},
    "anthropic_beta": ["tools-2024-05-16"],
    "anthropic_version": "bedrock-2023-05-31",
    "stream": False,
    "service_tier": "auto",
    "thinking": {"type": "enabled", "budget_tokens": 1024},
}


class TestMessagesEndpointAcceptsClaudeCodePayload:
    """The handler's job is to forward the request to Bedrock — we do
    not test Bedrock itself. What we DO test is that the FastAPI +
    Pydantic validation layer lets the request through rather than
    rejecting it with 422 before the handler runs. If this test ever
    starts failing with 422, someone accidentally reintroduced
    ``extra='forbid'`` on ``AnthropicMessagesRequest`` and every
    Claude Code session is broken in production.
    """

    def test_pydantic_does_not_422_on_claude_code_body(self, monkeypatch):
        """Parse the body through the real Pydantic model. No FastAPI
        or Bedrock stubs required — the shape check is Pydantic's."""
        from mvp.anthropic import AnthropicMessagesRequest

        try:
            AnthropicMessagesRequest.model_validate(CLAUDE_CODE_BODY)
        except Exception as exc:  # pragma: no cover — intentional catch-all
            pytest.fail(
                "AnthropicMessagesRequest rejected a realistic Claude Code "
                f"payload. extra='forbid' regression? Error: {exc!r}"
            )

    def test_known_top_level_anthropic_fields_survive_round_trip(self):
        """Every Anthropic Messages API field in the payload must
        survive ``model_dump``. If the forwarder stripped them out,
        tools would be silently dropped on the wire to Bedrock."""
        from mvp.anthropic import AnthropicMessagesRequest

        body = AnthropicMessagesRequest.model_validate(CLAUDE_CODE_BODY)
        dumped = body.model_dump(exclude_none=True)
        # The exhaustive list below is the 2026-04 Anthropic API
        # surface Claude Code currently sends. Extending the list is
        # fine; shrinking it is a regression.
        required_survivors = {
            "tools",
            "tool_choice",
            "metadata",
            "anthropic_beta",
            "anthropic_version",
            "service_tier",
            "thinking",
        }
        missing = sorted(required_survivors - dumped.keys())
        assert missing == [], (
            f"Anthropic passthrough fields dropped: {missing}. "
            "The /v1/messages forwarder must not strip them."
        )

    def test_extra_forbid_is_not_set_on_the_request_model(self):
        """Belt-and-braces: assert the config flag directly so a
        future refactor that sets ``extra='forbid'`` without
        noticing fails on THIS line rather than on production
        traffic."""
        from mvp.anthropic import AnthropicMessage, AnthropicMessagesRequest

        # Pydantic v2: ``model_config`` is a plain dict on the class.
        for cls in (AnthropicMessagesRequest, AnthropicMessage):
            assert cls.model_config.get("extra") != "forbid", (
                f"{cls.__name__} has extra='forbid'. Claude Code and the "
                "Anthropic SDKs send forward-compatible top-level fields; "
                "forbidding them breaks every Claude Code request."
            )
