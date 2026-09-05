"""C8.1 on the Anthropic `/v1/messages` wire (the other transport
`usage_from_bedrock` must cover — its docstring: "Used by both transports, so
the 'absent is not zero' rule cannot hold on one path and not the other").

`test_chat_completions_billing_contract.py` covers the OpenAI-compatible
`/v1/chat/completions` non-streaming path; this file is the same check against
`mvp.anthropic`'s non-streaming Converse handler, which has the identical
`int(usage.get("inputTokens", 0))` / `int(usage.get("outputTokens", 0))`
pattern at the time of writing (mvp/anthropic.py, just above the
`hold.claim_settle` call). Not implemented at `bb0fb2c`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mvp.anthropic import router as anthropic_router
from mvp.authz import _PERMS_CACHE
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
    """`_PERMS_CACHE` entries go through `monkeypatch.setitem`, not a bare
    assignment — see the identical fixture in
    `test_chat_completions_billing_contract.py` for why a bare assignment
    here is a cross-file pollution bug (it leaked into
    `test_contract_authority_source.py` in a full-suite run)."""
    monkeypatch.setitem(_PERMS_CACHE, "user", (["messages:send", "usage:read-self"], time.time() + 3600))
    monkeypatch.setitem(_PERMS_CACHE, "admin", (["messages:send", "usage:read-self", "tenants:update"], time.time() + 3600))

    app = FastAPI()
    app.include_router(anthropic_router)
    app.dependency_overrides[get_current_user] = lambda: _FakeUser()

    with patch("mvp.anthropic._bedrock_client") as mock_bedrock:
        yield TestClient(app), mock_bedrock


def test_anthropic_nonstreaming_response_with_no_usage_key_does_not_settle(api_client, monkeypatch):
    client, mock_bedrock = api_client
    mock_bedrock.return_value.converse.return_value = {
        "output": {"message": {"content": [{"text": "hi"}]}},
        "stopReason": "end_turn",
        # No "usage" key at all.
    }

    calls = {"n": 0}
    import mvp.anthropic as anth

    real_settle = anth._settle_reservation_and_log

    def _counting_settle(**kwargs):
        calls["n"] += 1
        return real_settle(**kwargs)

    monkeypatch.setattr(anth, "_settle_reservation_and_log", _counting_settle)

    resp = client.post("/v1/messages", json={
        "model": MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 50,
    })
    assert resp.status_code in (200, 502), (
        f"unexpected status for an unreadable-usage response: {resp.status_code}"
    )

    assert calls["n"] == 0, (
        "settle was invoked on the Anthropic /v1/messages path for a response "
        "with no usage key — usage_from_bedrock must gate BOTH transports, "
        "not just chat_completions"
    )
