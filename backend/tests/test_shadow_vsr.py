"""Unit tests for the minimal LOCAL shadow VSR judge (mvp.vsr.shadow).

The judge is PURE and injectable: given a requested model + cheap request
features, it advises a cheaper same-family model (or nothing), never steering
execution. These tests use fake resolvers so no registry/pricing import is needed;
one test exercises the real registry-backed defaults.
"""
from __future__ import annotations

from mvp.vsr import shadow

# fake tier map: model id -> {pricing_key, bedrock_model_id}
_FAKE = {
    "opus": {"pricing_key": "opus", "bedrock_model_id": "bid-opus"},
    "sonnet": {"pricing_key": "sonnet", "bedrock_model_id": "bid-sonnet"},
    "haiku": {"pricing_key": "haiku", "bedrock_model_id": "bid-haiku"},
    "mystery": {"pricing_key": "weird", "bedrock_model_id": "bid-mystery"},
}


def _resolve(m):
    return _FAKE.get(m)


def _cheapest(_tier):
    return "haiku"


def _simple(tokens=100):
    return shadow.RequestFeatures(approx_input_tokens=tokens, has_tools=False, has_images=False)


def _propose(model, features):
    return shadow.propose(requested_model=model, features=features,
                          resolve=_resolve, cheapest_model_for_tier=_cheapest)


def test_simple_expensive_request_is_advised_down_to_cheapest():
    s = _propose("opus", _simple())
    assert s is not None and s.model == "haiku" and s.rule_id == "R2-simple-downgrade"


def test_already_cheapest_is_not_advised():
    assert _propose("haiku", _simple()) is None


def test_tools_are_never_downgraded():
    f = shadow.RequestFeatures(approx_input_tokens=100, has_tools=True, has_images=False)
    assert _propose("opus", f) is None


def test_images_are_never_downgraded():
    f = shadow.RequestFeatures(approx_input_tokens=100, has_tools=False, has_images=True)
    assert _propose("opus", f) is None


def test_large_prompt_keeps_the_stronger_model():
    big = shadow.RequestFeatures(approx_input_tokens=999_999, has_tools=False, has_images=False)
    assert _propose("opus", big) is None


def test_unknown_or_unpriced_model_is_out_of_scope():
    assert _propose("nope", _simple()) is None            # not resolvable
    assert _propose("mystery", _simple()) is None          # resolvable but non-tier


def test_never_advises_the_same_model():
    # if the cheapest-for-tier resolves to the SAME bedrock id as requested, no advice.
    s = shadow.propose(requested_model="haiku", features=_simple(),
                       resolve=_resolve, cheapest_model_for_tier=lambda _t: "haiku")
    assert s is None


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("STRATOCLAVE_SHADOW_VSR", raising=False)
    assert shadow.shadow_enabled() is False
    monkeypatch.setenv("STRATOCLAVE_SHADOW_VSR", "true")
    assert shadow.shadow_enabled() is True


def test_real_registry_backed_defaults_downgrade_opus_to_haiku():
    # exercises the real resolver + cheapest-for-tier against the code registry.
    s = shadow.propose(requested_model="claude-opus-4-7", features=_simple())
    assert s is not None and s.rule_id == "R2-simple-downgrade"
    # the advised model resolves to the haiku tier.
    from mvp.models import resolve_model
    assert resolve_model(s.model).pricing_key == "haiku"


# --------------------------------------------- request-path wiring helper
# (Fable shadow-wiring review): shadow_vsr_decision is the seam the route
# handlers call. Dark by default, fail-open, produces a decision dict shaped like
# vsr.client.decision_record and classified into SHADOW_DECISIONS (potential only).

def test_shadow_vsr_decision_dark_by_default_never_calls_propose(monkeypatch):
    monkeypatch.delenv("STRATOCLAVE_SHADOW_VSR", raising=False)
    called = {"n": 0}
    monkeypatch.setattr(shadow, "propose",
                        lambda **kw: called.__setitem__("n", called["n"] + 1))
    assert shadow.shadow_vsr_decision(requested_model="claude-opus-4-7",
                                      features=_simple()) is None
    # "dark" = the judge NEVER runs, not "runs and is discarded".
    assert called["n"] == 0


def test_shadow_vsr_decision_shape_and_classification(monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_SHADOW_VSR", "true")
    d = shadow.shadow_vsr_decision(requested_model="claude-opus-4-7", features=_simple())
    assert d is not None
    # shape mirrors vsr.client.decision_record + rule_id + config_version.
    assert set(d) == {"decision", "suggested_model", "mode", "config_version", "rule_id"}
    assert d["mode"] == "shadow"
    assert d["config_version"] == shadow.SHADOW_CONFIG_VERSION
    assert d["rule_id"] == "R2-simple-downgrade"
    # the AUTHORITATIVE downstream key is `decision` — it must be the shadow label
    # so savings/reconcile put it in the potential (enacted=False) base, never realized.
    from mvp.vsr.client import DECISION_SHADOW_ADVISED, SHADOW_DECISIONS, STEERING_DECISIONS
    assert d["decision"] == DECISION_SHADOW_ADVISED
    assert d["decision"] in SHADOW_DECISIONS
    assert d["decision"] not in STEERING_DECISIONS


def test_shadow_vsr_decision_none_when_no_advice(monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_SHADOW_VSR", "true")
    # a request already at the cheapest tier -> propose returns None -> dict None.
    assert shadow.shadow_vsr_decision(requested_model="claude-haiku-4-5",
                                      features=_simple()) is None


def test_shadow_vsr_decision_fail_open_on_propose_error(monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_SHADOW_VSR", "true")

    def _boom(**kw):
        raise RuntimeError("registry down")
    monkeypatch.setattr(shadow, "propose", _boom)
    # any error yields None, never propagates.
    assert shadow.shadow_vsr_decision(requested_model="claude-opus-4-7",
                                      features=_simple()) is None


# --------------------------------------------- feature extractors (bools only)

def test_extract_features_openai_flags_tools_and_images():
    f = shadow.extract_features_openai(
        approx_input_tokens=200,
        tools=[{"type": "function"}],
        messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {}}]}])
    assert f.has_tools is True and f.has_images is True and f.approx_input_tokens == 200


def test_extract_features_openai_plain_text_has_no_capabilities():
    f = shadow.extract_features_openai(
        approx_input_tokens=50, tools=None,
        messages=[{"role": "user", "content": "hello"}])
    assert f.has_tools is False and f.has_images is False


def test_extract_features_anthropic_flags_image_parts():
    f = shadow.extract_features_anthropic(
        approx_input_tokens=10, tools=None,
        messages=[{"role": "user", "content": [{"type": "image", "source": {}}]}])
    assert f.has_images is True


def test_extract_features_conservative_on_bad_shape():
    # a message object that raises when iterated for content -> report image present.
    class _Bad:
        @property
        def content(self):
            raise ValueError("boom")
    f = shadow.extract_features_openai(approx_input_tokens=10, tools=None, messages=[_Bad()])
    assert f.has_images is True   # doubt => capability present => never downgrade


def test_savings_classification_is_mode_independent(monkeypatch):
    """Seal the design claim (Fable review-2 (b)): `decision` is authoritative for
    realized-vs-potential; `mode` is informational. Tampering with `mode` must NOT
    move a shadow decision out of the potential (enacted=False) base."""
    from mvp.learning.savings import counterfactual_row
    monkeypatch.setenv("STRATOCLAVE_SHADOW_VSR", "true")
    d = shadow.shadow_vsr_decision(requested_model="claude-opus-4-7", features=_simple())
    assert d is not None
    # a reconcile-join row carrying the shadow decision; mode deliberately corrupted.
    base = {"tenant_id": "t", "span_id": "s", "vsr_decision": d["decision"],
            "suggested_model": d["suggested_model"], "mode": "hard",  # <- lie
            "matched": True, "billed_model_id": "claude-opus-4-7",
            "cost_microusd": 1000, "input_tokens": 100, "output_tokens": 100}
    row = counterfactual_row(base)
    # classified by `decision` (shadow-advised) => advice only, never realized.
    assert row["enacted"] is False


# --------------------------------------------------- per-tenant shadow toggle
# (Fable per-tenant review): shadow_enabled resolves tri-state — tenant explicit
# wins; None falls back to the global env default. shadow.py stays free of any
# storage dependency (the caller passes the resolved bool).

def test_shadow_enabled_tenant_true_overrides_env_off(monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_SHADOW_VSR", "false")   # global OFF
    assert shadow.shadow_enabled(True) is True             # tenant opted in


def test_shadow_enabled_tenant_false_overrides_env_on(monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_SHADOW_VSR", "true")    # global ON
    assert shadow.shadow_enabled(False) is False           # tenant opted out


def test_shadow_enabled_none_follows_global_default(monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_SHADOW_VSR", "true")
    assert shadow.shadow_enabled(None) is True
    monkeypatch.setenv("STRATOCLAVE_SHADOW_VSR", "false")
    assert shadow.shadow_enabled(None) is False
    monkeypatch.delenv("STRATOCLAVE_SHADOW_VSR", raising=False)
    assert shadow.shadow_enabled(None) is False            # dark by default
    # legacy no-arg call (existing callers) still resolves to the global default.
    assert shadow.shadow_enabled() is False


def test_shadow_vsr_decision_respects_tenant_off(monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_SHADOW_VSR", "true")    # global ON...
    # ...but this tenant is explicitly OFF -> no advisory even for a downgradable req.
    assert shadow.shadow_vsr_decision(
        requested_model="claude-opus-4-7", features=_simple(),
        tenant_shadow=False) is None


def test_shadow_vsr_decision_tenant_on_beats_env_off(monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_SHADOW_VSR", "false")   # global OFF...
    # ...tenant explicitly ON -> advisory is produced.
    d = shadow.shadow_vsr_decision(
        requested_model="claude-opus-4-7", features=_simple(), tenant_shadow=True)
    assert d is not None and d["decision"]
