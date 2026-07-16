"""P0-15: VSR hard-pin edge extractor + passthrough recipe invariant.

The pin's selection/allowlist/quota behaviour is covered in test_quota_cascade
(TestVsrHardPin); this file covers the edge grammar (extract_model_pin) and the
documented passthrough recipe (a degenerate routing config always serves the
requested model, no fallback), plus an HTTP-boundary test of the pin.
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from hypothesis import given, strategies as st

from mvp.anthropic import router as anthropic_router
from mvp.deps import HDR_MODEL_PIN, extract_model_pin, get_current_user


# ----------------------------------------------------------------- extractor

def _fake_request(headers: dict) -> Request:
    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


def test_extract_pin_absent_returns_none():
    assert extract_model_pin(_fake_request({})) is None


def test_extract_pin_blank_is_none():
    assert extract_model_pin(_fake_request({HDR_MODEL_PIN: "  "})) is None


def test_extract_pin_valid_passes_through():
    r = _fake_request({HDR_MODEL_PIN: "us.anthropic.claude-haiku-4-5"})
    assert extract_model_pin(r) == "us.anthropic.claude-haiku-4-5"


@pytest.mark.parametrize("bad", [
    "has space", "with#hash", "a" * 129, "line\nbreak", "semi;colon",
])
def test_extract_pin_malformed_is_400(bad):
    with pytest.raises(HTTPException) as e:
        extract_model_pin(_fake_request({HDR_MODEL_PIN: bad}))
    assert e.value.status_code == 400
    assert e.value.detail["reason"] == "invalid_model_pin"


@given(pin=st.from_regex(r"\A[A-Za-z0-9._:/-]{1,128}\Z"))
def test_extract_pin_grammar_accepts_valid(pin):
    assert extract_model_pin(_fake_request({HDR_MODEL_PIN: pin})) == pin


# ------------------------------------------------------- passthrough recipe

class TestPassthroughRecipe:
    """The documented passthrough recipe: no chain, no quotas, fallback off ->
    resolve_model returns the requested model unchanged, no fallback, even under
    a breaker cap. Locking this as a test so future routing changes can't
    silently break the recipe."""

    def _resolve(self, requested, breaker_max_tier=None):
        from mvp.routing.model_resolver import RoutingConfig, resolve_model
        cfg = RoutingConfig(chain=(), allowlist=(), quotas={},
                            fallback_default="off", free_tier_model=None)
        return resolve_model(
            requested_model=requested, tenant_config=cfg,
            breaker_max_tier=breaker_max_tier, fallback_allowed=False)

    @given(model=st.sampled_from([
        "claude-sonnet-4-6", "claude-haiku-4-5", "claude-opus-4-7"]))
    def test_passthrough_serves_requested_unchanged(self, model):
        r = self._resolve(model)
        assert r.selected_model == model
        assert r.fallback_occurred is False

    @given(model=st.sampled_from(["claude-sonnet-4-6", "claude-haiku-4-5"]),
           tier=st.integers(min_value=0, max_value=3))
    def test_passthrough_holds_under_breaker_cap(self, model, tier):
        r = self._resolve(model, breaker_max_tier=tier)
        assert r.selected_model == model
        assert r.fallback_occurred is False


# ------------------------------------------------------- HTTP boundary

@dataclass
class _FakeUser:
    user_id: str = "user-vsr-1"
    org_id: str = "vsr-org"
    email: str = "t@example.com"
    roles: list = None
    auth_kind: str = "jwt"
    key_scopes: list = None

    def __post_init__(self):
        if self.roles is None:
            self.roles = ["user"]


def _mock_converse(**kwargs):
    return {"output": {"message": {"content": [{"text": "hi"}]}},
            "stopReason": "end_turn", "usage": {"inputTokens": 3, "outputTokens": 2}}


@pytest.fixture
def api_client(dynamodb_mock, monkeypatch):
    import mvp.authz as _authz
    monkeypatch.setattr(_authz, "user_has_permission", lambda user, perm: True)
    from dynamo.user_tenants import UserTenantsRepository
    UserTenantsRepository().ensure(user_id=_FakeUser().user_id,
                                   tenant_id=_FakeUser().org_id, role="user",
                                   total_credit=10**9)
    app = FastAPI()
    app.include_router(anthropic_router)
    app.dependency_overrides[get_current_user] = lambda: _FakeUser()
    with patch("mvp.anthropic._bedrock_client") as mb:
        mb.return_value.converse.side_effect = _mock_converse
        yield TestClient(app)


def _post(client, headers=None):
    return client.post("/v1/messages", headers=headers or {}, json={
        "model": "us.anthropic.claude-opus-4-7",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 40, "stream": False,
    })


class TestPinHttp:
    def test_valid_pin_serves_200(self, api_client):
        # no allowlist configured -> any registry-servable pin is allowed
        resp = _post(api_client, headers={HDR_MODEL_PIN: "claude-haiku-4-5"})
        assert resp.status_code == 200

    def test_malformed_pin_400(self, api_client):
        resp = _post(api_client, headers={HDR_MODEL_PIN: "bad pin"})
        assert resp.status_code == 400
        assert resp.json()["detail"]["reason"] == "invalid_model_pin"

    def test_unservable_pin_400(self, api_client):
        resp = _post(api_client, headers={HDR_MODEL_PIN: "no-such-model"})
        assert resp.status_code == 400

    def test_no_pin_unchanged(self, api_client):
        resp = _post(api_client)
        assert resp.status_code == 200
