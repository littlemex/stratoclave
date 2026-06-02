"""OpenAI Responses route — model registry, scope gating, feature flag.

These tests exercise the route surface end-to-end against a mocked
authentication layer:

  - GET /openai/v1/models requires `responses:send` and lists only
    `provider="openai"` entries from the registry.
  - A `messages:send`-scoped API key cannot reach `POST /openai/v1/responses`.
  - When `CODEX_ENABLED` is unset/false, the route returns HTTP 503.
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mvp.deps import AuthenticatedUser, get_current_user
from mvp.models import _REGISTRY


def _user_with_scopes(scopes: list[str]) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="user-test",
        email="t@example",
        org_id="default-org",
        roles=["user"],
        raw_claims={},
        auth_kind="api_key",
        key_scopes=scopes,
        api_key_hash="dummy-hash",
    )


def _patch_authz(monkeypatch, allow: set[str]) -> None:
    """Bypass DynamoDB Permissions for these tests by monkeypatching
    `mvp.authz.user_has_permission` to consult a static set."""
    from mvp import authz

    def fake_user_has_permission(user, scope: str) -> bool:
        # API-key callers must intersect both role-perm and key-scope; for
        # this test we model that the key-scope is the binding constraint
        # (`allow` is the simulated key_scopes set).
        if user.auth_kind == "api_key":
            if user.key_scopes is None:
                return False
            return scope in user.key_scopes
        return scope in allow

    monkeypatch.setattr(authz, "user_has_permission", fake_user_has_permission)


def _make_app(monkeypatch, *, scope_holder: list[str], codex_enabled: bool) -> TestClient:
    """Build a FastAPI app exposing only the OpenAI Responses router with
    auth + RBAC stubbed out. `scope_holder[0]` is the scope set returned
    by the mocked authentication."""
    monkeypatch.setenv("CODEX_ENABLED", "true" if codex_enabled else "false")
    monkeypatch.setenv("DEFAULT_CODEX_MODEL", "openai.gpt-5.4")

    _patch_authz(monkeypatch, allow={"responses:send"})

    from mvp.openai_responses import router

    app = FastAPI()
    app.include_router(router)

    def _override_get_current_user() -> AuthenticatedUser:
        return _user_with_scopes(scope_holder)

    app.dependency_overrides[get_current_user] = _override_get_current_user
    return TestClient(app)


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------


def test_codex_disabled_returns_503(monkeypatch):
    client = _make_app(monkeypatch, scope_holder=["responses:send"], codex_enabled=False)
    resp = client.get("/openai/v1/models")
    assert resp.status_code == 503
    resp = client.post(
        "/openai/v1/responses",
        json={"model": "openai.gpt-5.4", "input": "hi", "max_output_tokens": 4},
    )
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Models endpoint
# ---------------------------------------------------------------------------


def test_openai_models_endpoint_scope_gating(monkeypatch):
    """A key without `responses:send` cannot probe the endpoint."""
    client = _make_app(monkeypatch, scope_holder=["messages:send"], codex_enabled=True)
    resp = client.get("/openai/v1/models")
    assert resp.status_code == 403


def test_openai_models_lists_only_openai_provider(monkeypatch):
    client = _make_app(monkeypatch, scope_holder=["responses:send"], codex_enabled=True)
    resp = client.get("/openai/v1/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    ids = {row["id"] for row in body["data"]}
    # Every listed id must resolve to a provider="openai" entry.
    openai_aliases = {
        alias for entry in _REGISTRY for alias in entry.aliases
        if entry.provider == "openai"
    }
    assert ids == openai_aliases
    # Crucially, no Claude-family ids leak through.
    assert "claude-opus-4-7" not in ids


# ---------------------------------------------------------------------------
# Scope isolation between /v1/messages and /openai/v1/responses
# ---------------------------------------------------------------------------


def test_messages_send_scope_cannot_reach_responses(monkeypatch):
    """A `messages:send`-only key must hit 403 on /openai/v1/responses."""
    client = _make_app(monkeypatch, scope_holder=["messages:send"], codex_enabled=True)
    resp = client.post(
        "/openai/v1/responses",
        json={"model": "openai.gpt-5.4", "input": "hi", "max_output_tokens": 4},
    )
    assert resp.status_code == 403


def test_responses_send_only_passes_scope_layer(monkeypatch):
    """A `responses:send`-only key must clear the scope check.

    Past the scope check we hit the credit-reservation step, which
    needs DynamoDB; we short-circuit it by stubbing `reserve_credit`
    to raise an HTTPException(402) so the test proves the scope
    layer let us through without depending on moto.
    """
    from fastapi import HTTPException

    def fake_reserve_credit(user, reservation_tokens):
        raise HTTPException(402, "stub: scope check passed; aborting before bedrock")

    monkeypatch.setattr(
        "mvp.openai_responses.reserve_credit", fake_reserve_credit
    )

    client = _make_app(monkeypatch, scope_holder=["responses:send"], codex_enabled=True)
    resp = client.post(
        "/openai/v1/responses",
        json={"model": "openai.gpt-5.4", "input": "hi", "max_output_tokens": 4},
    )
    # 402 from the stub means the scope check passed and we reached
    # reserve_credit; 403 (scope reject) or 422 (schema reject) would
    # be regressions.
    assert resp.status_code == 402
