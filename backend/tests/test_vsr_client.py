"""External VSR version-pin + fail-open consult (task #13).

Proves the version pin is ENFORCED at runtime (not documentary) and that the
consult is inert/off-by-default and fail-open:

  * flag OFF          => consult() and handshake() are no-ops (UNVERIFIED),
    no HTTP client built;
  * version match      => VERIFIED, consult returns a parsed suggestion;
  * contract/build mismatch => REFUSED, zero consults honored, routing = today;
  * unreachable        => UNVERIFIED (degrade, auto-heals), never a 500;
  * per-response contract-header mismatch => response discarded, state -> REFUSED;
  * malformed / non-200 / bad shape => None (fail-open).

A fake httpx transport feeds /version and /v1/route — no real VSR.
"""
from __future__ import annotations

import httpx
import pytest

from mvp.vsr import client as vsr


def _install(monkeypatch, *, flag=True, contract="vsr/1", builds="1.4.2",
             base="http://vsr:9000"):
    monkeypatch.setenv("EXTERNAL_VSR_ENABLED", "true" if flag else "false")
    monkeypatch.setenv("VSR_BASE_URL", base)
    monkeypatch.setenv("VSR_EXPECTED_CONTRACT", contract)
    monkeypatch.setenv("VSR_EXPECTED_BUILDS", builds)
    vsr.reset_for_test()


def _fake_client(handler) -> httpx.Client:
    return httpx.Client(base_url="http://vsr:9000",
                        transport=httpx.MockTransport(handler))


def test_flag_off_is_inert(monkeypatch):
    _install(monkeypatch, flag=False)
    # No client should ever be built; handshake stays UNVERIFIED, consult None.
    assert vsr.handshake() == vsr.UNVERIFIED
    assert vsr.consult(tenant_id="t", session_key="s", requested_model="m") is None


def test_version_match_verifies_and_consult_returns_suggestion(monkeypatch):
    _install(monkeypatch)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/version":
            return httpx.Response(200, json={"contract": "vsr/1", "version": "1.4.2",
                                             "build": "abc"})
        if req.url.path == "/v1/route":
            return httpx.Response(200, headers={"x-vsr-contract": "vsr/1"},
                                  json={"pin_model": "claude-haiku-4-5", "mode": "prefer"})
        return httpx.Response(404)

    monkeypatch.setattr(vsr, "_get_client", lambda: _fake_client(handler))
    assert vsr.handshake() == vsr.VERIFIED
    s = vsr.consult(tenant_id="t", session_key="s", requested_model="claude-opus-4-7")
    assert s is not None
    assert s.model == "claude-haiku-4-5"
    assert s.mode == "prefer"


def test_build_mismatch_refuses_and_consult_blocked(monkeypatch):
    _install(monkeypatch, builds="1.4.2")

    def handler(req):
        if req.url.path == "/version":
            return httpx.Response(200, json={"contract": "vsr/1", "version": "9.9.9"})
        raise AssertionError("consult must not be attempted when REFUSED")

    monkeypatch.setattr(vsr, "_get_client", lambda: _fake_client(handler))
    assert vsr.handshake() == vsr.REFUSED
    # A REFUSED task still serves — consult just returns None, no HTTP call.
    assert vsr.consult(tenant_id="t", session_key="s", requested_model="m") is None


def test_contract_mismatch_refuses(monkeypatch):
    _install(monkeypatch, contract="vsr/1")

    def handler(req):
        return httpx.Response(200, json={"contract": "vsr/2", "version": "1.4.2"})

    monkeypatch.setattr(vsr, "_get_client", lambda: _fake_client(handler))
    assert vsr.handshake() == vsr.REFUSED


def test_unreachable_degrades_to_unverified(monkeypatch):
    _install(monkeypatch)

    def handler(req):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(vsr, "_get_client", lambda: _fake_client(handler))
    assert vsr.handshake() == vsr.UNVERIFIED
    assert vsr.consult(tenant_id="t", session_key="s", requested_model="m") is None


def test_per_response_contract_header_mismatch_discards_and_refuses(monkeypatch):
    _install(monkeypatch)
    calls = {"version": 0}

    def handler(req):
        if req.url.path == "/version":
            calls["version"] += 1
            return httpx.Response(200, json={"contract": "vsr/1", "version": "1.4.2"})
        # consult returns a WRONG contract header (mid-flight redeploy).
        return httpx.Response(200, headers={"x-vsr-contract": "vsr/2"},
                              json={"pin_model": "m", "mode": "hard"})

    monkeypatch.setattr(vsr, "_get_client", lambda: _fake_client(handler))
    assert vsr.handshake() == vsr.VERIFIED
    assert vsr.consult(tenant_id="t", session_key="s", requested_model="m") is None
    # The bad consult flipped state to REFUSED pending re-handshake.
    assert vsr.get_state() == vsr.REFUSED


def test_malformed_suggestion_is_failopen(monkeypatch):
    _install(monkeypatch)

    def handler(req):
        if req.url.path == "/version":
            return httpx.Response(200, json={"contract": "vsr/1", "version": "1.4.2"})
        # 200 + right header but a nonsense body / bad mode.
        return httpx.Response(200, headers={"x-vsr-contract": "vsr/1"},
                              json={"pin_model": "", "mode": "sideways"})

    monkeypatch.setattr(vsr, "_get_client", lambda: _fake_client(handler))
    assert vsr.handshake() == vsr.VERIFIED
    assert vsr.consult(tenant_id="t", session_key="s", requested_model="m") is None


def test_empty_pinned_config_fails_closed(monkeypatch):
    # No expected contract/builds set => nothing can match => REFUSED even if the
    # VSR advertises a version.
    _install(monkeypatch, contract="", builds="")

    def handler(req):
        return httpx.Response(200, json={"contract": "vsr/1", "version": "1.4.2"})

    monkeypatch.setattr(vsr, "_get_client", lambda: _fake_client(handler))
    assert vsr.handshake() == vsr.REFUSED


# --------------------------------------------------------------------------
# consult_ex: consult-time OUTCOME for observability (Fable design first step).
# consult() stays a thin wrapper (contract preserved); consult_ex adds the
# reason so the caller can emit one honest decision log line — WITHOUT
# re-implementing the VSR's own routing metrics.
# --------------------------------------------------------------------------

def test_consult_ex_flag_off(monkeypatch):
    _install(monkeypatch, flag=False)
    r = vsr.consult_ex(tenant_id="t", session_key="s", requested_model="m")
    assert r.outcome == vsr.CONSULT_FLAG_OFF
    assert r.suggestion is None


def test_consult_ex_unverified_when_not_handshaked(monkeypatch):
    # Flag on but no successful handshake => state UNVERIFIED => consult skipped.
    _install(monkeypatch)
    r = vsr.consult_ex(tenant_id="t", session_key="s", requested_model="m")
    assert r.outcome == vsr.CONSULT_UNVERIFIED
    assert r.suggestion is None


def test_consult_ex_timeout_on_transport_error(monkeypatch):
    _install(monkeypatch)

    def handler(req):
        if req.url.path == "/version":
            return httpx.Response(200, json={"contract": "vsr/1", "version": "1.4.2"})
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(vsr, "_get_client", lambda: _fake_client(handler))
    assert vsr.handshake() == vsr.VERIFIED
    r = vsr.consult_ex(tenant_id="t", session_key="s", requested_model="m")
    assert r.outcome == vsr.CONSULT_TIMEOUT
    assert r.suggestion is None


def test_consult_ex_no_advice_on_bad_shape(monkeypatch):
    _install(monkeypatch)

    def handler(req):
        if req.url.path == "/version":
            return httpx.Response(200, json={"contract": "vsr/1", "version": "1.4.2"})
        return httpx.Response(200, headers={"x-vsr-contract": "vsr/1"},
                              json={"pin_model": "", "mode": "sideways"})

    monkeypatch.setattr(vsr, "_get_client", lambda: _fake_client(handler))
    assert vsr.handshake() == vsr.VERIFIED
    r = vsr.consult_ex(tenant_id="t", session_key="s", requested_model="m")
    assert r.outcome == vsr.CONSULT_NO_ADVICE
    assert r.suggestion is None


def test_consult_ex_suggested_carries_suggestion(monkeypatch):
    _install(monkeypatch)

    def handler(req):
        if req.url.path == "/version":
            return httpx.Response(200, json={"contract": "vsr/1", "version": "1.4.2"})
        return httpx.Response(200, headers={"x-vsr-contract": "vsr/1"},
                              json={"pin_model": "claude-haiku-4-5", "mode": "hard"})

    monkeypatch.setattr(vsr, "_get_client", lambda: _fake_client(handler))
    assert vsr.handshake() == vsr.VERIFIED
    r = vsr.consult_ex(tenant_id="t", session_key="s", requested_model="claude-opus-4-7")
    assert r.outcome == vsr.CONSULT_SUGGESTED
    assert r.suggestion is not None
    assert r.suggestion.model == "claude-haiku-4-5"
    assert r.suggestion.mode == "hard"
    # Backward-compat: consult() still returns just the suggestion.
    s = vsr.consult(tenant_id="t", session_key="s", requested_model="claude-opus-4-7")
    assert s is not None and s.model == "claude-haiku-4-5"


# --------------------------------------------------------------------------
# classify_consult_decision: PURE mapping consult-result -> logged label.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("outcome", [
    vsr.CONSULT_FLAG_OFF, vsr.CONSULT_UNVERIFIED,
    vsr.CONSULT_TIMEOUT, vsr.CONSULT_NO_ADVICE,
])
def test_classify_passes_through_non_suggested(outcome):
    r = vsr.VsrConsultResult(outcome)
    # No suggestion => the raw outcome is the label, regardless of SAAR state.
    assert vsr.classify_consult_decision(r, saar_prefer_present=False) == outcome
    assert vsr.classify_consult_decision(r, saar_prefer_present=True) == outcome


def test_classify_hard_is_applied():
    r = vsr.VsrConsultResult(vsr.CONSULT_SUGGESTED,
                             vsr.VsrSuggestion(model="m", mode="hard"))
    # A hard pin wins regardless of any local SAAR prefer.
    assert vsr.classify_consult_decision(r, saar_prefer_present=False) == vsr.DECISION_HARD_APPLIED
    assert vsr.classify_consult_decision(r, saar_prefer_present=True) == vsr.DECISION_HARD_APPLIED


def test_classify_prefer_applied_vs_overridden():
    r = vsr.VsrConsultResult(vsr.CONSULT_SUGGESTED,
                             vsr.VsrSuggestion(model="m", mode="prefer"))
    # No local SAAR prefer => the VSR prefer takes the cascade head.
    assert vsr.classify_consult_decision(r, saar_prefer_present=False) == vsr.DECISION_PREFER_APPLIED
    # A local SAAR prefer already holds it => the VSR prefer is overridden this turn.
    assert vsr.classify_consult_decision(r, saar_prefer_present=True) == vsr.DECISION_PREFER_OVERRIDDEN


# --------------------------------------------------------------------------
# consult_ex config-version echo (skew-detection contract) + the pure
# decision_record / decision_headers builders (decision-log + response headers).
# --------------------------------------------------------------------------

def test_consult_ex_captures_config_version_echo(monkeypatch):
    _install(monkeypatch)

    def handler(req):
        if req.url.path == "/version":
            return httpx.Response(200, json={"contract": "vsr/1", "version": "1.4.2"})
        return httpx.Response(
            200,
            headers={"x-vsr-contract": "vsr/1", "x-vsr-config-version": "cfgv-abc123"},
            json={"pin_model": "claude-haiku-4-5", "mode": "hard"},
        )

    monkeypatch.setattr(vsr, "_get_client", lambda: _fake_client(handler))
    assert vsr.handshake() == vsr.VERIFIED
    r = vsr.consult_ex(tenant_id="t", session_key="s", requested_model="m")
    assert r.outcome == vsr.CONSULT_SUGGESTED
    assert r.config_version == "cfgv-abc123"


def test_consult_ex_config_version_none_when_not_echoed(monkeypatch):
    _install(monkeypatch)

    def handler(req):
        if req.url.path == "/version":
            return httpx.Response(200, json={"contract": "vsr/1", "version": "1.4.2"})
        return httpx.Response(200, headers={"x-vsr-contract": "vsr/1"},
                              json={"pin_model": "m", "mode": "prefer"})

    monkeypatch.setattr(vsr, "_get_client", lambda: _fake_client(handler))
    assert vsr.handshake() == vsr.VERIFIED
    r = vsr.consult_ex(tenant_id="t", session_key="s", requested_model="m")
    assert r.config_version is None  # older VSR omits it -> skew simply undetected


def test_decision_record_shape():
    r = vsr.VsrConsultResult(vsr.CONSULT_SUGGESTED,
                             vsr.VsrSuggestion(model="claude-haiku-4-5", mode="hard"),
                             config_version="cfgv-1")
    rec = vsr.decision_record(r, saar_prefer_present=False)
    assert rec == {
        "decision": vsr.DECISION_HARD_APPLIED,
        "suggested_model": "claude-haiku-4-5",
        "mode": "hard",
        "config_version": "cfgv-1",
    }


def test_decision_record_no_advice_has_only_decision():
    r = vsr.VsrConsultResult(vsr.CONSULT_NO_ADVICE)
    assert vsr.decision_record(r, saar_prefer_present=False) == {"decision": "no-advice"}


def test_decision_headers_shape():
    r = vsr.VsrConsultResult(vsr.CONSULT_SUGGESTED,
                             vsr.VsrSuggestion(model="claude-sonnet-4-6", mode="prefer"),
                             config_version="cfgv-9")
    h = vsr.decision_headers(r, saar_prefer_present=False)
    assert h[vsr.HDR_VSR_DECISION] == vsr.DECISION_PREFER_APPLIED
    assert h[vsr.HDR_VSR_SUGGESTED] == "claude-sonnet-4-6"
    assert h[vsr.HDR_VSR_CONFIG_VERSION] == "cfgv-9"


def test_decision_headers_prefer_overridden_carries_no_suggested_when_absent():
    # A non-suggested outcome yields just the decision header (no model leak).
    r = vsr.VsrConsultResult(vsr.CONSULT_TIMEOUT)
    h = vsr.decision_headers(r, saar_prefer_present=False)
    assert h == {vsr.HDR_VSR_DECISION: "timeout"}
